"""Unit tests for DCR proxy registration_not_supported path."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nextcloud_mcp_server.auth.oauth_routes import oauth_register_proxy

pytestmark = pytest.mark.unit


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
        "nextcloud_mcp_server.auth.oauth_routes._get_cached_discovery",
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
