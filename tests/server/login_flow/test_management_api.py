"""Integration tests for the management API on the login-flow MCP server.

These tests drive a real OAuth flow against Nextcloud's `oidc` app using the
static `astrolabeMcpClientOAuth00000000000` client (which is allowlisted on
the `mcp-login-flow` container via `ALLOWED_MGMT_CLIENT` and matches the id
provisioned by `app-hooks/before-starting/26-configure-astrolabe-oauth.sh`
in real deployments), then hit the management API endpoints with the
resulting bearer token.

Regression coverage for the bug where /api/v1/apps proxied to OCS v1
/cloud/apps and always 401'd. The handler now uses /ocs/v2.php/cloud/capabilities,
which is reachable for OAuth bearer tokens.
"""

import httpx
import pytest

LOGIN_FLOW_API_BASE_URL = "http://localhost:8004"

pytestmark = [pytest.mark.integration, pytest.mark.login_flow]


async def test_get_installed_apps_returns_capability_keys(
    login_flow_static_client_token: str,
):
    """GET /api/v1/apps returns 200 with a list of enabled-app capability keys."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{LOGIN_FLOW_API_BASE_URL}/api/v1/apps",
            headers={"Authorization": f"Bearer {login_flow_static_client_token}"},
        )

    assert response.status_code == 200, (
        f"/api/v1/apps returned {response.status_code}: {response.text}"
    )

    data = response.json()
    assert "apps" in data
    assert isinstance(data["apps"], list)

    # Anonymous capabilities always exposes core; authenticated also exposes
    # files. Both should be present whether or not the oidc app's
    # BearerAuthMiddleware ran for this OCS route.
    apps = data["apps"]
    assert "core" in apps, f"expected 'core' in apps, got {apps}"
    assert "files" in apps, f"expected 'files' in apps, got {apps}"


async def test_get_installed_apps_requires_bearer_token():
    """No Authorization header → 401 (handler's token validator rejects it)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{LOGIN_FLOW_API_BASE_URL}/api/v1/apps")

    assert response.status_code == 401
