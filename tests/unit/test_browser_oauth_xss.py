"""Regression tests for HTML XSS in browser OAuth error responses.

The reviewer on PR #758 flagged that ``oauth_login_callback`` interpolated
IdP-controlled and query-parameter-controlled text into HTMLResponse bodies
without escaping. These tests pin the html_escape behavior so the
vulnerability cannot regress silently.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from cryptography.fernet import Fernet

from nextcloud_mcp_server.auth import token_utils
from nextcloud_mcp_server.auth.browser_oauth_routes import oauth_login_callback
from nextcloud_mcp_server.auth.storage import RefreshTokenStorage

pytestmark = pytest.mark.unit


XSS_PAYLOAD = "<script>alert(1)</script>"


@pytest.fixture(autouse=True)
def _clear_oidc_discovery_cache():
    """Reset the shared discovery cache between tests."""
    token_utils._discovery_cache.clear()
    yield
    token_utils._discovery_cache.clear()


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "xss.db"
        s = RefreshTokenStorage(
            db_path=str(db_path), encryption_key=Fernet.generate_key().decode()
        )
        await s.initialize()
        yield s


def _build_request(*, query_params: dict, oauth_context: dict | None = None):
    request = MagicMock()
    request.query_params = query_params
    request.cookies = {}
    request.app.state.oauth_context = oauth_context
    request.url_for = MagicMock(return_value="/oauth/login")
    return request


async def test_callback_escapes_error_query_params(storage):
    """`error` and `error_description` are attacker-controlled — must be escaped."""
    request = _build_request(
        query_params={
            "error": XSS_PAYLOAD,
            "error_description": XSS_PAYLOAD,
        },
        oauth_context={"storage": storage, "config": {}},
    )

    response = await oauth_login_callback(request)
    body = response.body.decode()

    assert XSS_PAYLOAD not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


async def test_callback_does_not_reflect_idp_http_error_body(storage):
    """IdP-returned HTTPError body must not appear in the user-visible HTML.

    Updated for PR #758 round-3 nit 6: the callback now logs the IdP
    response server-side and shows the user only a generic message + a
    correlation ID, eliminating reflection of attacker-controllable text
    into the error page entirely.
    """
    discovery = {"token_endpoint": "http://idp.example/token"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx2.Response(
                200,
                content=json.dumps(discovery).encode(),
                headers={"content-type": "application/json"},
            )
        if str(request.url) == "http://idp.example/token":
            return httpx2.Response(400, content=XSS_PAYLOAD.encode())
        return httpx2.Response(404)

    transport = httpx2.MockTransport(handler)

    def fake_client(**kwargs):
        kwargs["transport"] = transport
        return httpx2.AsyncClient(**kwargs)

    # Pre-populate the oauth_session row that the callback expects
    await storage.store_oauth_session(
        session_id="state-xss",
        client_id="browser-ui",
        client_redirect_uri="/app",
        state="state-xss",
        code_challenge="cc",
        code_challenge_method="S256",
        mcp_authorization_code="cv",
        flow_type="browser",
        ttl_seconds=600,
    )

    request = _build_request(
        query_params={"code": "abc", "state": "state-xss"},
        oauth_context={
            "storage": storage,
            "oauth_client": None,
            "config": {
                "discovery_url": "http://idp.example/.well-known/openid-configuration",
                "client_id": "test",
                "client_secret": "secret",
                "mcp_server_url": "http://localhost",
            },
        },
    )

    # Discovery now goes through token_utils.get_oidc_discovery (PR #758
    # round-2 nit 3); token-exchange POST still uses browser_oauth_routes'
    # httpx2 client.
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
        response = await oauth_login_callback(request)

    body = response.body.decode()
    assert response.status_code == 500
    # Strict: neither the raw payload nor an HTML-escaped form of the
    # IdP body should appear — the page must show only the generic
    # message + correlation ID.
    assert XSS_PAYLOAD not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" not in body
    assert "An internal error occurred" in body
    assert "Correlation ID" in body
