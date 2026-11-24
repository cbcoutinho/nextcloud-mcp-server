"""DCR proxy against a real Nextcloud: public clients with a loopback redirect.

Browser-based inspectors (MCPJam) and CLI MCP clients register as public
clients (``token_endpoint_auth_method: none``) with a localhost redirect and no
``application_type``. Nextcloud's oidc app treats that as a web client and
answers 400 invalid_redirect_uri unless the proxy marks it native.
"""

import os

import httpx2
import pytest

from nextcloud_mcp_server.auth.client_registration import delete_client

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]

MCP_URL = "http://localhost:8004"


async def test_public_loopback_client_registers_through_proxy():
    async with httpx2.AsyncClient(timeout=30) as http:
        response = await http.post(
            f"{MCP_URL}/oauth/register",
            json={
                "client_name": "dcr-public-loopback-test",
                "redirect_uris": ["http://localhost:6274/oauth/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
    assert response.status_code == 201, response.text
    client = response.json()
    try:
        assert client["client_id"]
    finally:
        await delete_client(
            nextcloud_url=os.environ["NEXTCLOUD_HOST"],
            client_id=client["client_id"],
            registration_access_token=client.get("registration_access_token"),
            client_secret=client.get("client_secret"),
            # Not registration_client_uri: through the proxy it names the
            # container-internal Nextcloud host, which the test runner can't resolve.
        )
