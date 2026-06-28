"""Unit tests for OAuth logout (issue #626 finding 4) and the
SessionAuthBackend (finding 2).

These cover the new server-side session lifecycle:
  - logout deletes refresh token + browser session
  - logout calls IdP revocation_endpoint when available
  - logout still succeeds when IdP/storage errors
  - SessionAuthBackend resolves random session_id -> user_id, fails
    closed when the session is unknown / expired / has no refresh token
"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from starlette.requests import HTTPConnection

from nextcloud_mcp_server.auth import token_utils
from nextcloud_mcp_server.auth.browser_oauth_routes import (
    _revoke_refresh_token_at_idp,
    oauth_logout,
)
from nextcloud_mcp_server.auth.session_backend import SessionAuthBackend
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_oidc_discovery_cache():
    """Reset the shared discovery cache so tests don't see each other's fetches.

    ``_revoke_refresh_token_at_idp`` was changed (PR #758 nit 6) to use
    ``token_utils.get_oidc_discovery`` which caches for 5 minutes — without
    this clear, the second test in the file would see the first test's
    discovery doc and skip the MockTransport call.
    """
    token_utils._discovery_cache.clear()
    yield
    token_utils._discovery_cache.clear()


# ---------------------------------------------------------------------------
# storage fixture (real SQLite backend; lighter than mocking every call)
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_logout.db"
        s = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=Fernet.generate_key().decode()
        )
        await s.initialize()
        yield s


def _build_request(
    *,
    cookie: str | None,
    oauth_context: dict | None,
    headers: dict | None = None,
):
    """Build a minimal Starlette-style request stub for oauth_logout."""
    request = MagicMock()
    request.query_params = {}
    request.cookies = {"mcp_session": cookie} if cookie else {}
    request.app.state.oauth_context = oauth_context
    # Headers default to empty so the CSRF check sees neither Origin nor
    # Referer (allowed by policy — see _origin_matches_self).
    request.headers = headers or {}
    return request


# ---------------------------------------------------------------------------
# oauth_logout
# ---------------------------------------------------------------------------


async def test_logout_deletes_refresh_token_and_session(storage):
    """Happy path: logout removes the refresh token and the browser session."""
    await storage.create_browser_session(session_id="sid-1", user_id="alice")
    await storage.store_refresh_token(
        user_id="alice", refresh_token="rt-abc", flow_type="browser"
    )

    request = _build_request(
        cookie="sid-1",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
    )

    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes._revoke_refresh_token_at_idp",
        new=AsyncMock(),
    ):
        response = await oauth_logout(request)

    assert response.status_code == 302
    assert await storage.get_refresh_token("alice") is None
    assert await storage.get_browser_session_user("sid-1") is None


async def test_logout_calls_revocation_when_refresh_token_present(storage):
    """The IdP revocation helper is called with the stored refresh token."""
    await storage.create_browser_session(session_id="sid-2", user_id="bob")
    await storage.store_refresh_token(
        user_id="bob", refresh_token="rt-xyz", flow_type="browser"
    )

    revoke = AsyncMock()
    request = _build_request(
        cookie="sid-2",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": "http://idp/.well-known",
            },
        },
    )

    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes._revoke_refresh_token_at_idp",
        new=revoke,
    ):
        await oauth_logout(request)

    revoke.assert_awaited_once()
    args = revoke.await_args.args
    # Second arg is the refresh token string
    assert args[1] == "rt-xyz"


async def test_logout_no_session_cookie_returns_302(storage):
    """Without a cookie, logout still 302s and doesn't touch storage."""
    request = _build_request(
        cookie=None,
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
    )
    response = await oauth_logout(request)
    assert response.status_code == 302


async def test_logout_swallows_storage_errors(storage):
    """Logout is best-effort — a storage failure must not 500 the response."""
    await storage.create_browser_session(session_id="sid-3", user_id="carol")
    broken_storage = MagicMock()
    broken_storage.get_browser_session_user = AsyncMock(
        side_effect=RuntimeError("db down")
    )
    broken_storage.delete_browser_session = AsyncMock()

    request = _build_request(
        cookie="sid-3",
        oauth_context={
            "storage": broken_storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
    )
    response = await oauth_logout(request)
    assert response.status_code == 302  # logout still succeeds


async def test_logout_deletes_session_when_refresh_token_delete_fails(storage):
    """Browser session row must be removed even if delete_refresh_token raises.

    Pins PR #758 round-5 review medium 1: previously the two deletes lived
    in the same try-block, so an error on ``delete_refresh_token`` left an
    orphan ``browser_sessions`` row that lingered until the cleanup cron.
    """
    await storage.create_browser_session(session_id="sid-orphan", user_id="dave")
    await storage.store_refresh_token(
        user_id="dave", refresh_token="rt-dave", flow_type="browser"
    )

    real_delete_refresh_token = storage.delete_refresh_token
    real_delete_browser_session = storage.delete_browser_session

    storage.delete_refresh_token = AsyncMock(side_effect=RuntimeError("boom"))
    delete_browser_session_calls: list[str] = []

    async def tracking_delete_browser_session(session_id: str) -> bool:
        delete_browser_session_calls.append(session_id)
        return await real_delete_browser_session(session_id)

    storage.delete_browser_session = tracking_delete_browser_session

    request = _build_request(
        cookie="sid-orphan",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
    )

    try:
        response = await oauth_logout(request)
    finally:
        storage.delete_refresh_token = real_delete_refresh_token
        storage.delete_browser_session = real_delete_browser_session

    assert response.status_code == 302
    assert delete_browser_session_calls == ["sid-orphan"], (
        "delete_browser_session must run even after delete_refresh_token raised"
    )
    assert await storage.get_browser_session_user("sid-orphan") is None, (
        "browser_sessions row must be gone — finally branch failed to fire"
    )


async def test_logout_blocks_cross_origin_post(storage):
    """POST from a foreign Origin must be rejected with 403 (PR #758 finding 5)."""
    await storage.create_browser_session(session_id="sid-X", user_id="alice")

    request = _build_request(
        cookie="sid-X",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
        headers={"origin": "https://evil.example.com"},
    )

    response = await oauth_logout(request)
    assert response.status_code == 403
    # Session row must NOT have been deleted.
    assert await storage.get_browser_session_user("sid-X") == "alice"


async def test_logout_allows_same_origin_post(storage):
    """POST with matching Origin proceeds normally."""
    await storage.create_browser_session(session_id="sid-Y", user_id="alice")

    request = _build_request(
        cookie="sid-Y",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
        headers={"origin": "https://mcp.example.com"},
    )

    response = await oauth_logout(request)
    assert response.status_code == 302
    assert await storage.get_browser_session_user("sid-Y") is None


async def test_logout_allows_same_origin_post_with_explicit_default_port(storage):
    """mcp_server_url has explicit :443; browser Origin omits the port.

    RFC 6454 §6.2: browsers omit default ports in Origin headers. The
    netloc string ``mcp.example.com:443`` would never match ``mcp.example.com``
    without port normalisation, blocking every legitimate logout.
    """
    await storage.create_browser_session(session_id="sid-PE", user_id="alice")

    request = _build_request(
        cookie="sid-PE",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com:443",
                "discovery_url": None,
            },
        },
        headers={"origin": "https://mcp.example.com"},
    )

    response = await oauth_logout(request)
    assert response.status_code == 302
    assert await storage.get_browser_session_user("sid-PE") is None


async def test_logout_allows_same_origin_post_with_default_port_in_origin(storage):
    """Symmetric case: config omits port, Origin includes :443."""
    await storage.create_browser_session(session_id="sid-PI", user_id="alice")

    request = _build_request(
        cookie="sid-PI",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
        headers={"origin": "https://mcp.example.com:443"},
    )

    response = await oauth_logout(request)
    assert response.status_code == 302
    assert await storage.get_browser_session_user("sid-PI") is None


async def test_logout_blocks_scheme_mismatch(storage):
    """Same hostname but different scheme must be treated as cross-origin."""
    await storage.create_browser_session(session_id="sid-SC", user_id="alice")

    request = _build_request(
        cookie="sid-SC",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
        headers={"origin": "http://mcp.example.com"},
    )

    response = await oauth_logout(request)
    assert response.status_code == 403
    assert await storage.get_browser_session_user("sid-SC") == "alice"


async def test_logout_allows_referer_when_origin_missing(storage):
    """Some browsers strip Origin on POST; Referer is the fallback signal."""
    await storage.create_browser_session(session_id="sid-Z", user_id="alice")

    request = _build_request(
        cookie="sid-Z",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
        headers={"referer": "https://mcp.example.com/app"},
    )

    response = await oauth_logout(request)
    assert response.status_code == 302


async def test_logout_blocked_when_mcp_server_url_missing(storage):
    """Fail-closed CSRF (PR #758 round-3 finding 2): missing ``mcp_server_url``
    in oauth_ctx must reject the logout, not allow it.

    A future code path that leaves ``mcp_server_url`` unset would
    otherwise silently disable CSRF protection. Blocking is recoverable.
    """
    await storage.create_browser_session(session_id="sid-MM", user_id="alice")

    request = _build_request(
        cookie="sid-MM",
        oauth_context={"storage": storage, "config": {"discovery_url": None}},
    )

    response = await oauth_logout(request)
    assert response.status_code == 403
    # Session must NOT have been deleted.
    assert await storage.get_browser_session_user("sid-MM") == "alice"


async def test_logout_handles_session_with_no_refresh_token(storage):
    """Cookie + session row exist but refresh token already gone — logout is idempotent."""
    await storage.create_browser_session(session_id="sid-4", user_id="dave")

    revoke = AsyncMock()
    request = _build_request(
        cookie="sid-4",
        oauth_context={
            "storage": storage,
            "config": {
                "mcp_server_url": "https://mcp.example.com",
                "discovery_url": None,
            },
        },
    )
    with patch(
        "nextcloud_mcp_server.auth.browser_oauth_routes._revoke_refresh_token_at_idp",
        new=revoke,
    ):
        await oauth_logout(request)

    # Revoke not called — no token to revoke
    revoke.assert_not_called()
    # Browser session still cleared
    assert await storage.get_browser_session_user("sid-4") is None


# ---------------------------------------------------------------------------
# _revoke_refresh_token_at_idp
# ---------------------------------------------------------------------------


def _httpx_handler(routes: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return handler


async def test_revoke_helper_posts_to_revocation_endpoint():
    discovery_url = "http://idp.example/.well-known"
    revocation_url = "http://idp.example/revoke"

    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == discovery_url:
            return httpx.Response(
                200,
                content=json.dumps({"revocation_endpoint": revocation_url}).encode(),
                headers={"content-type": "application/json"},
            )
        if str(request.url) == revocation_url:
            received.append(request)
            return httpx.Response(200)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    # Discovery now goes through token_utils.get_oidc_discovery (PR #758 nit
    # 6); revocation POST still uses browser_oauth_routes' httpx client.
    with (
        patch(
            "nextcloud_mcp_server.auth.browser_oauth_routes.nextcloud_httpx_client",
            side_effect=fake_client,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
            side_effect=fake_client,
        ),
    ):
        await _revoke_refresh_token_at_idp(
            {
                "config": {
                    "discovery_url": discovery_url,
                    "client_id": "test-client",
                    "client_secret": "test-secret",
                }
            },
            "rt-secret",
        )

    assert len(received) == 1
    body = received[0].content.decode()
    assert "token=rt-secret" in body
    assert "token_type_hint=refresh_token" in body


async def test_revoke_helper_skips_when_no_revocation_endpoint():
    """IdPs without a revocation_endpoint advertised: helper must no-op silently."""
    discovery_url = "http://idp.example/.well-known"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == discovery_url:
            return httpx.Response(200, json={})  # no revocation_endpoint
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    with (
        patch(
            "nextcloud_mcp_server.auth.browser_oauth_routes.nextcloud_httpx_client",
            side_effect=fake_client,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
            side_effect=fake_client,
        ),
    ):
        # Returns None and does not raise
        result = await _revoke_refresh_token_at_idp(
            {
                "config": {
                    "discovery_url": discovery_url,
                    "client_id": "x",
                    "client_secret": "y",
                }
            },
            "rt",
        )
    assert result is None


async def test_revoke_helper_silent_on_idp_error():
    """If the IdP 500s, the helper must not raise — caller treats it as best-effort."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    with (
        patch(
            "nextcloud_mcp_server.auth.browser_oauth_routes.nextcloud_httpx_client",
            side_effect=fake_client,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.nextcloud_httpx_client",
            side_effect=fake_client,
        ),
    ):
        result = await _revoke_refresh_token_at_idp(
            {
                "config": {
                    "discovery_url": "http://x/.well-known",
                    "client_id": "x",
                    "client_secret": "y",
                }
            },
            "rt",
        )
    assert result is None


# ---------------------------------------------------------------------------
# SessionAuthBackend
# ---------------------------------------------------------------------------


def _build_conn(*, cookie: str | None, oauth_context: dict | None):
    conn = MagicMock(spec=HTTPConnection)
    conn.cookies = {"mcp_session": cookie} if cookie else {}
    conn.url = SimpleNamespace(path="/app")
    conn.app = MagicMock()
    conn.app.state.oauth_context = oauth_context
    return conn


async def test_session_backend_authenticates_known_session_with_token(storage):
    await storage.create_browser_session(session_id="sid-A", user_id="alice")
    await storage.store_refresh_token(
        user_id="alice", refresh_token="rt", flow_type="browser"
    )

    backend = SessionAuthBackend(oauth_enabled=True)
    conn = _build_conn(cookie="sid-A", oauth_context={"storage": storage})

    result = await backend.authenticate(conn)
    assert result is not None
    creds, user = result
    assert "authenticated" in creds.scopes
    assert user.username == "alice"


async def test_session_backend_rejects_unknown_session(storage):
    backend = SessionAuthBackend(oauth_enabled=True)
    conn = _build_conn(cookie="not-a-real-sid", oauth_context={"storage": storage})
    assert await backend.authenticate(conn) is None


async def test_session_backend_rejects_session_without_refresh_token(storage):
    """Defense-in-depth: session row exists but user has no refresh token.

    PR #758 round-7 minor: rejection now also evicts the orphaned
    ``browser_sessions`` row so the table doesn't accumulate dead entries
    that the auth check will keep rejecting until TTL cleanup.
    """
    await storage.create_browser_session(session_id="sid-B", user_id="bob")
    # Note: NO refresh token stored for bob

    backend = SessionAuthBackend(oauth_enabled=True)
    conn = _build_conn(cookie="sid-B", oauth_context={"storage": storage})
    assert await backend.authenticate(conn) is None

    # Orphan must be evicted on rejection.
    assert await storage.get_browser_session_user("sid-B") is None


async def test_session_backend_rejects_when_no_cookie(storage):
    backend = SessionAuthBackend(oauth_enabled=True)
    conn = _build_conn(cookie=None, oauth_context={"storage": storage})
    assert await backend.authenticate(conn) is None


async def test_session_backend_basicauth_mode_short_circuits(monkeypatch, storage):
    """In BasicAuth mode (oauth_enabled=False) the backend never touches storage."""
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "admin-user")
    # refresh dynaconf so the env mutation above is seen
    from nextcloud_mcp_server.config import _reload_config

    _reload_config()
    backend = SessionAuthBackend(oauth_enabled=False)
    conn = _build_conn(cookie=None, oauth_context=None)
    result = await backend.authenticate(conn)
    assert result is not None
    _, user = result
    assert user.username == "admin-user"
