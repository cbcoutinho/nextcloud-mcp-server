"""Unit tests for DCR proxy: static client short-circuit and registration_not_supported."""

import json
import string
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st

import nextcloud_mcp_server.auth.client_registry as registry_mod
from nextcloud_mcp_server.auth.oauth_routes import (
    _with_native_application_type,
    oauth_register_proxy,
)
from tests.strategies import (
    dictionaries_with_optional_keys,
    fixed_draw_lists,
    fixed_draw_one_of,
    json_values,
)

pytestmark = pytest.mark.unit

_PUBLIC = {"token_endpoint_auth_method": "none"}


# --- application_type for public loopback clients (MCPJam, browser inspectors) ---


_words = st.text(string.ascii_lowercase, min_size=1, max_size=6)
_ports = st.none() | st.integers(1, 65535)
_paths = st.lists(_words, max_size=3).map(lambda p: "/" + "/".join(p))


def _http(host: str, port: int | None, path: str) -> str:
    return f"http://{host}{f':{port}' if port else ''}{path}"


# RFC 8252 §7.3 loopback redirects and §7.1 private-use (reverse-DNS) schemes.
_native_uris = st.builds(
    _http, st.sampled_from(["localhost", "127.0.0.1", "[::1]"]), _ports, _paths
) | st.builds(lambda a, b, path: f"com.{a}.{b}:{path}", _words, _words, _paths)

# Everything a browser can open, plus loopback lookalikes.
_web_uris = (
    st.builds(
        _http,
        st.sampled_from(["localhost.evil.example", "127.0.0.2", "app.example.com"]),
        _ports,
        _paths,
    )
    | st.builds(
        lambda host, path: f"https://{host}{path}",
        st.sampled_from(["app.example.com", "localhost", "127.0.0.1"]),
        _paths,
    )
    | st.builds(
        lambda scheme, rest: f"{scheme}:{rest}",
        st.sampled_from(["javascript", "data", "file", "vbscript"]),
        _words,
    )
)


@st.composite
def registrations(draw):
    """``(body, expected application_type)``: a DCR body and how it is forwarded.

    Native only for a public client with no application_type whose redirects
    are all native; otherwise forwarded unchanged.
    """
    native = draw(st.booleans())
    uris = draw(st.lists(_native_uris, min_size=1, max_size=3))
    if not native:
        breaker = draw(st.sampled_from(["web_uri", "no_uris", "confidential", "typed"]))
        event(f"breaker: {breaker}")
        if breaker == "web_uri":
            uris.insert(draw(st.integers(0, len(uris))), draw(_web_uris))
        elif breaker == "no_uris":
            uris = []
    body = {"token_endpoint_auth_method": "none", "redirect_uris": uris}
    if not native and breaker == "confidential":
        body["token_endpoint_auth_method"] = draw(
            st.sampled_from(["client_secret_basic", "client_secret_post", "None"])
        )
    if not native and breaker == "typed":
        body["application_type"] = draw(st.sampled_from(["web", "native", ""]))
    event(f"native: {native}")
    return body, "native" if native else body.get("application_type")


@given(registrations())
@example(
    ({**_PUBLIC, "redirect_uris": ["http://localhost:6274/oauth/callback"]}, "native")
)  # MCPJam
@example(({**_PUBLIC, "redirect_uris": ["http://localhost.evil.example/cb"]}, None))
def test_native_application_type(case):
    body, expected = case
    assert _with_native_application_type(body).get("application_type") == expected


@given(
    dictionaries_with_optional_keys(
        {},
        dict.fromkeys(
            ["token_endpoint_auth_method", "redirect_uris", "application_type", "x"],
            fixed_draw_one_of(
                json_values, st.just("none"), fixed_draw_lists(st.text(), max_size=5)
            ),
        ),
    )
)
@example({**_PUBLIC, "redirect_uris": ["http://[::1/cb"]})  # invalid IPv6 host
def test_native_application_type_never_raises(body):
    out = _with_native_application_type(body)
    # Only ever adds application_type; every other field is forwarded as-is.
    rest = {k: v for k, v in out.items() if k != "application_type"}
    assert rest == {k: v for k, v in body.items() if k != "application_type"}


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the singleton registry before each test."""
    registry_mod._registry = None
    yield
    registry_mod._registry = None


def _make_request(body: dict, oauth_config: dict) -> MagicMock:
    """Create a mock Starlette Request."""
    request = AsyncMock()
    request.json = AsyncMock(return_value=body)
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    request.app = MagicMock()
    request.app.state.oauth_context = {"config": oauth_config}
    return request


_DCR_BODY = {
    "client_name": "test",
    "redirect_uris": ["http://localhost:9999/cb"],
}


async def test_registration_not_supported_when_no_endpoint():
    """When discovery doc lacks registration_endpoint, return 400."""
    request = _make_request(
        body=_DCR_BODY,
        oauth_config={
            "discovery_url": "https://idp.example.com/.well-known/openid-configuration"
        },
    )

    discovery_doc = {
        "issuer": "https://idp.example.com",
        "authorization_endpoint": "https://idp.example.com/auth",
    }

    with patch(
        "nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery",
        new_callable=AsyncMock,
        return_value=discovery_doc,
    ):
        response = await oauth_register_proxy(request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"] == "registration_not_supported"
    assert "ALLOWED_MCP_CLIENTS" in body["error_description"]


async def test_registration_not_supported_when_no_discovery_url():
    """When no discovery_url is configured, return 400."""
    request = _make_request(body=_DCR_BODY, oauth_config={})

    response = await oauth_register_proxy(request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["error"] == "registration_not_supported"


# ---------------------------------------------------------------------------
# Static client short-circuit tests
# ---------------------------------------------------------------------------


async def test_static_client_match_skips_idp_proxy(monkeypatch):
    """DCR for localhost redirect URI returns pre-configured static client without proxying IdP."""
    monkeypatch.setenv("ALLOWED_MCP_CLIENTS", "claude-code-mcp")
    from nextcloud_mcp_server.config import _reload_config

    _reload_config()

    request = _make_request(
        body={
            "client_name": "Claude Code (nextcloud)",
            "redirect_uris": ["http://localhost:54321/callback"],
        },
        oauth_config={
            "discovery_url": "https://idp.example.com/.well-known/openid-configuration"
        },
    )

    # get_oidc_discovery must NOT be called — the short-circuit fires first
    with patch(
        "nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery",
        new_callable=AsyncMock,
    ) as mock_discovery:
        response = await oauth_register_proxy(request)

    mock_discovery.assert_not_called()
    assert response.status_code == 201
    body = json.loads(response.body)
    assert body["client_id"] == "claude-code-mcp"
    assert body["redirect_uris"] == ["http://localhost:54321/callback"]
    assert body["token_endpoint_auth_method"] == "none"
    assert "client_id_issued_at" in body


async def test_static_client_match_different_port_returns_same_client_id(monkeypatch):
    """Two DCR requests with different localhost ports return the same static client_id."""
    monkeypatch.setenv("ALLOWED_MCP_CLIENTS", "claude-code-mcp")
    from nextcloud_mcp_server.config import _reload_config

    _reload_config()

    oauth_config = {
        "discovery_url": "https://idp.example.com/.well-known/openid-configuration"
    }

    with patch("nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery"):
        resp1 = await oauth_register_proxy(
            _make_request(
                body={
                    "client_name": "Claude Code",
                    "redirect_uris": ["http://localhost:11111/cb"],
                },
                oauth_config=oauth_config,
            )
        )
        resp2 = await oauth_register_proxy(
            _make_request(
                body={
                    "client_name": "Claude Code",
                    "redirect_uris": ["http://localhost:22222/cb"],
                },
                oauth_config=oauth_config,
            )
        )

    body1 = json.loads(resp1.body)
    body2 = json.loads(resp2.body)
    assert body1["client_id"] == body2["client_id"]
    # client_id_issued_at must be stable (sourced from the static MCPClientInfo record,
    # not from the current request time) so both responses agree on the same value.
    assert body1["client_id_issued_at"] == body2["client_id_issued_at"]


async def test_no_static_client_falls_through_to_idp_proxy(monkeypatch):
    """When no static client matches the redirect URI, the IdP proxy path is taken."""
    monkeypatch.delenv("ALLOWED_MCP_CLIENTS", raising=False)
    from nextcloud_mcp_server.config import _reload_config

    _reload_config()

    request = _make_request(
        body=_DCR_BODY,
        oauth_config={
            "discovery_url": "https://idp.example.com/.well-known/openid-configuration"
        },
    )

    discovery_doc = {
        "issuer": "https://idp.example.com",
        "authorization_endpoint": "https://idp.example.com/auth",
        # No registration_endpoint → falls through to 400
    }

    with patch(
        "nextcloud_mcp_server.auth.oauth_routes.get_oidc_discovery",
        new_callable=AsyncMock,
        return_value=discovery_doc,
    ) as mock_discovery:
        response = await oauth_register_proxy(request)

    mock_discovery.assert_called_once()
    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "registration_not_supported"
