"""Refresh tokens handed out by the AS proxy are bound to their client.

The proxy refreshes against the IdP with the MCP server's own credentials, so
the IdP cannot tell which MCP client a refresh token belongs to. Before the
binding existed, ``grant_type=refresh_token`` accepted a token from any
``client_id``, or from none (RFC 6749 §6). The load-bearing tests here are the
negative ones: a refresh from the wrong client, or from no client, must be
refused *before* the IdP is contacted. Proxying first would rotate the token
and leave its real owner holding a dead one.

Parametrized over both storage backends, like the other storage tests (SQLite
always; Postgres when ``TEST_DATABASE_URL`` is set).
"""

import base64
import hashlib
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth import oauth_routes
from nextcloud_mcp_server.auth.oauth_routes import (
    _REFRESH_BINDING_DEFAULT_TTL,
    ProxyCodeEntry,
    _refresh_binding_ttl,
    _refresh_token_hash,
)
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit

_CLIENT = "https://claude.ai/oauth/mcp-oauth-client-metadata"
_OTHER_CLIENT = "https://evil.example.com/cimd.json"


@pytest.fixture
async def storage(storage_backend):
    key = Fernet.generate_key().decode()
    if storage_backend["kind"] == "sqlite":
        with tempfile.TemporaryDirectory() as tmpdir:
            s = RefreshTokenStorage(
                db_path=str(Path(tmpdir) / "bindings.db"), encryption_key=key
            )
            await s.initialize()
            yield s
    else:
        s = RefreshTokenStorage(database_url=storage_backend["url"], encryption_key=key)
        await s.initialize()
        try:
            yield s
        finally:
            await storage_backend["reset"]()


# ---------------------------------------------------------------------------
# Storage contract
# ---------------------------------------------------------------------------


async def test_bind_and_get(storage):
    await storage.bind_refresh_token_client("h1", _CLIENT, ttl_seconds=3600)
    assert await storage.get_refresh_token_client("h1") == _CLIENT


async def test_unknown_hash_is_unbound(storage):
    assert await storage.get_refresh_token_client("nope") is None


async def test_rebind_replaces_client(storage):
    await storage.bind_refresh_token_client("h1", _CLIENT, ttl_seconds=3600)
    await storage.bind_refresh_token_client("h1", _OTHER_CLIENT, ttl_seconds=3600)
    assert await storage.get_refresh_token_client("h1") == _OTHER_CLIENT


async def test_expired_binding_is_unbound_and_deleted(storage):
    # ttl -2: expires_at strictly in the past (the check is ``<``).
    await storage.bind_refresh_token_client("h1", _CLIENT, ttl_seconds=-2)
    assert await storage.get_refresh_token_client("h1") is None
    assert await storage.delete_refresh_token_client("h1") is False


async def test_bind_sweeps_expired_rows(storage):
    """Rows of clients that never refresh again are never looked up, so the
    lazy delete in get_refresh_token_client alone would never remove them."""
    await storage.bind_refresh_token_client("stale", _CLIENT, ttl_seconds=-2)
    await storage.bind_refresh_token_client("fresh", _CLIENT, ttl_seconds=3600)

    assert await storage.delete_refresh_token_client("stale") is False
    assert await storage.get_refresh_token_client("fresh") == _CLIENT


async def test_delete(storage):
    await storage.bind_refresh_token_client("h1", _CLIENT, ttl_seconds=3600)
    assert await storage.delete_refresh_token_client("h1") is True
    assert await storage.get_refresh_token_client("h1") is None


# ---------------------------------------------------------------------------
# Binding lifetime
# ---------------------------------------------------------------------------


def test_ttl_follows_the_idp():
    assert _refresh_binding_ttl({"refresh_expires_in": 1209600}) == 1209600


@pytest.mark.parametrize("value", [None, 0, -5, "3600", True, 1.5])
def test_ttl_falls_back_when_the_idp_does_not_say(value):
    response = {} if value is None else {"refresh_expires_in": value}
    assert _refresh_binding_ttl(response) == _REFRESH_BINDING_DEFAULT_TTL


def test_hash_does_not_contain_the_token():
    token = "REFRESH-TOKEN-SECRET-4f8a2c1e9b7d6350"
    digest = _refresh_token_hash(token)
    assert token not in digest
    assert digest == _refresh_token_hash(token)
    assert digest != _refresh_token_hash(token + "x")


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


class _IdP:
    """Stands in for the Nextcloud token endpoint and records every call."""

    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self.body = body or {}
        self.calls: list[dict] = []

    @asynccontextmanager
    async def client(self):
        idp = self

        class _Client:
            async def post(self, url, data):
                idp.calls.append(data)
                return SimpleNamespace(
                    status_code=idp.status_code,
                    json=lambda: dict(idp.body),
                    text="",
                )

        yield _Client()


@pytest.fixture
def idp(mocker):
    idp = _IdP(
        body={
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "refresh_expires_in": 1209600,
            "token_type": "Bearer",
        }
    )
    mocker.patch.object(oauth_routes, "nextcloud_httpx_client", idp.client)
    mocker.patch.object(
        oauth_routes,
        "get_oidc_discovery",
        return_value={"token_endpoint": "http://idp.test/token"},
    )
    return idp


def _request(storage, form: dict[str, str], headers: dict[str, str] | None = None):
    """Minimal Starlette-like request carrying the OAuth context and *form*."""
    from starlette.datastructures import FormData, Headers

    oauth_context = {
        "storage": storage,
        "config": {
            "client_id": "mcp-server-client",
            "client_secret": "mcp-server-secret",
            "mcp_server_url": "http://mcp.test",
            "discovery_url": "http://idp.test/.well-known/openid-configuration",
        },
    }

    class _Req:
        app = SimpleNamespace(state=SimpleNamespace(oauth_context=oauth_context))

        def __init__(self):
            self.headers = Headers(headers or {})

        async def form(self):
            return FormData(form)

    return _Req()


async def _refresh(storage, refresh_token, client_id=None, headers=None):
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_id is not None:
        form["client_id"] = client_id
    return await oauth_routes.oauth_token_endpoint(_request(storage, form, headers))


async def test_authorization_code_grant_binds_the_refresh_token(storage):
    verifier = "v" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    oauth_routes._proxy_codes["code-1"] = ProxyCodeEntry(
        client_id=_CLIENT,
        client_redirect_uri="https://claude.ai/api/mcp/auth_callback",
        client_state="state",
        code_challenge=challenge,
        code_challenge_method="S256",
        nc_token_response={"access_token": "a", "refresh_token": "issued-refresh"},
    )
    response = await oauth_routes.oauth_token_endpoint(
        _request(
            storage,
            {
                "grant_type": "authorization_code",
                "code": "code-1",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "code_verifier": verifier,
                "client_id": _CLIENT,
            },
        )
    )

    assert response.status_code == 200
    stored = await storage.get_refresh_token_client(
        _refresh_token_hash("issued-refresh")
    )
    assert stored == _CLIENT


async def test_refresh_from_another_client_is_refused_before_the_idp(storage, idp):
    await storage.bind_refresh_token_client(
        _refresh_token_hash("rt"), _CLIENT, ttl_seconds=3600
    )

    response = await _refresh(storage, "rt", client_id=_OTHER_CLIENT)

    assert response.status_code == 400
    assert b"invalid_grant" in response.body
    assert idp.calls == [], "the IdP was contacted and the token rotated"
    # The real owner can still use its token.
    assert await storage.get_refresh_token_client(_refresh_token_hash("rt")) == _CLIENT


async def test_refresh_without_client_id_is_refused_before_the_idp(storage, idp):
    await storage.bind_refresh_token_client(
        _refresh_token_hash("rt"), _CLIENT, ttl_seconds=3600
    )

    response = await _refresh(storage, "rt")

    assert response.status_code == 400
    assert b"invalid_request" in response.body
    assert idp.calls == []


async def test_refresh_from_the_owner_rotates_the_binding(storage, idp):
    await storage.bind_refresh_token_client(
        _refresh_token_hash("rt"), _CLIENT, ttl_seconds=3600
    )

    response = await _refresh(storage, "rt", client_id=_CLIENT)

    assert response.status_code == 200
    assert len(idp.calls) == 1
    # The IdP still sees only the MCP server's own client.
    assert idp.calls[0]["client_id"] == "mcp-server-client"
    assert (
        await storage.get_refresh_token_client(_refresh_token_hash("rotated-refresh"))
        == _CLIENT
    )
    assert await storage.get_refresh_token_client(_refresh_token_hash("rt")) is None


async def test_client_id_from_basic_auth_is_accepted(storage, idp):
    await storage.bind_refresh_token_client(
        _refresh_token_hash("rt"), _CLIENT, ttl_seconds=3600
    )
    # RFC 6749 §2.3.1: the client_id is form-urlencoded before Basic
    # encoding, which matters for a CIMD client_id, since a URL has colons.
    basic = base64.b64encode(f"{quote(_CLIENT, safe='')}:secret".encode()).decode()

    response = await _refresh(
        storage, "rt", headers={"Authorization": f"Basic {basic}"}
    )

    assert response.status_code == 200


async def test_non_rotating_idp_keeps_the_binding(storage, idp):
    idp.body = {"access_token": "new-access", "token_type": "Bearer"}
    await storage.bind_refresh_token_client(
        _refresh_token_hash("rt"), _CLIENT, ttl_seconds=3600
    )

    response = await _refresh(storage, "rt", client_id=_CLIENT)

    assert response.status_code == 200
    assert await storage.get_refresh_token_client(_refresh_token_hash("rt")) == _CLIENT


async def test_failed_refresh_leaves_the_binding_alone(storage, idp):
    idp.status_code = 400
    await storage.bind_refresh_token_client(
        _refresh_token_hash("rt"), _CLIENT, ttl_seconds=3600
    )

    response = await _refresh(storage, "rt", client_id=_CLIENT)

    assert response.status_code == 400
    assert await storage.get_refresh_token_client(_refresh_token_hash("rt")) == _CLIENT
    assert (
        await storage.get_refresh_token_client(_refresh_token_hash("rotated-refresh"))
        is None
    )


async def test_unbound_token_is_claimed_by_its_first_client(storage, idp):
    """Tokens issued before the upgrade have no binding. They keep working,
    and the first refresh binds the rotated token to that client."""
    response = await _refresh(storage, "legacy-rt", client_id=_CLIENT)

    assert response.status_code == 200
    assert (
        await storage.get_refresh_token_client(_refresh_token_hash("rotated-refresh"))
        == _CLIENT
    )

    response = await _refresh(storage, "rotated-refresh", client_id=_OTHER_CLIENT)
    assert response.status_code == 400
    assert len(idp.calls) == 1
