"""Unit tests for DCR proxy: static client short-circuit and registration_not_supported."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import nextcloud_mcp_server.auth.client_registry as registry_mod
from nextcloud_mcp_server.auth.oauth_routes import oauth_register_proxy

pytestmark = pytest.mark.unit


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
