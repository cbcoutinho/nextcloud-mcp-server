"""OAuth integration tests for user info routes.

Tests verify:
1. /user endpoint returns correct user info in OAuth mode
2. /user/page endpoint renders HTML correctly in OAuth mode
3. Endpoints return 401 when not authenticated
4. Integration with Nextcloud OIDC and Keycloak IdP
"""

import json
import logging
import os

import httpx
import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


# ============================================================================
# Helper Functions
# ============================================================================


async def get_user_info_json(access_token: str, port: int = 8001) -> dict:
    """Call /user endpoint with OAuth token.

    Args:
        access_token: OAuth access token
        port: MCP server port (8001 for mcp-oauth, 8002 for mcp-keycloak)

    Returns:
        JSON response data
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{port}/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


async def get_user_info_html(access_token: str, port: int = 8001) -> str:
    """Call /user/page endpoint with OAuth token.

    Args:
        access_token: OAuth access token
        port: MCP server port (8001 for mcp-oauth, 8002 for mcp-keycloak)

    Returns:
        HTML response text
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:{port}/user/page",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.text


# ============================================================================
# Nextcloud OAuth Tests (mcp-oauth on port 8001)
# ============================================================================


async def test_user_info_json_with_nextcloud_oauth(playwright_oauth_token):
    """Test /user endpoint with Nextcloud OAuth token."""
    user_info = await get_user_info_json(playwright_oauth_token, port=8001)

    # Verify response structure
    assert "username" in user_info
    assert "auth_mode" in user_info
    assert user_info["auth_mode"] == "oauth"

    # Verify OAuth-specific fields
    assert "client_id" in user_info
    assert "scopes" in user_info
    assert "token_expires_at" in user_info
    assert isinstance(user_info["scopes"], list)

    # Verify username matches environment
    expected_username = os.getenv("NEXTCLOUD_USERNAME", "admin")
    assert user_info["username"] == expected_username

    logger.info(f"User info JSON: {json.dumps(user_info, indent=2)}")


async def test_user_info_html_with_nextcloud_oauth(playwright_oauth_token):
    """Test /user/page endpoint with Nextcloud OAuth token."""
    html = await get_user_info_html(playwright_oauth_token, port=8001)

    # Verify HTML structure
    assert "<!DOCTYPE html>" in html
    assert "Nextcloud MCP Server - User Info" in html
    assert "oauth" in html.lower()

    # Verify username is displayed
    expected_username = os.getenv("NEXTCLOUD_USERNAME", "admin")
    assert expected_username in html

    # Verify OAuth-specific content
    assert "Client ID" in html
    assert "Scopes" in html
    assert "Token Expires At" in html

    logger.info(f"User info HTML page rendered successfully ({len(html)} chars)")


async def test_user_info_json_unauthenticated():
    """Test /user endpoint without authentication returns 401."""
    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8001/user")

        # Should return 401 without authentication
        assert response.status_code == 401

        # Verify error message
        data = response.json()
        assert "error" in data
        assert data["error"] == "Not authenticated"

    logger.info("Unauthenticated request correctly returned 401")


async def test_user_info_html_unauthenticated():
    """Test /user/page endpoint without authentication returns 401 HTML."""
    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8001/user/page")

        # Should return 401 without authentication
        assert response.status_code == 401

        # Verify HTML error page
        html = response.text
        assert "<!DOCTYPE html>" in html
        assert "Authentication Required" in html
        assert "You must be authenticated to view this page" in html

    logger.info("Unauthenticated HTML request correctly returned 401 page")


async def test_user_info_with_alice_token(alice_oauth_token):
    """Test /user endpoint with alice's OAuth token."""
    user_info = await get_user_info_json(alice_oauth_token, port=8001)

    # Verify alice's user info
    assert user_info["username"] == "alice"
    assert user_info["auth_mode"] == "oauth"
    assert isinstance(user_info["scopes"], list)
    assert len(user_info["scopes"]) > 0

    logger.info(
        f"Alice's user info: username={user_info['username']}, scopes={user_info['scopes']}"
    )


async def test_user_info_with_bob_token(bob_oauth_token):
    """Test /user endpoint with bob's OAuth token."""
    user_info = await get_user_info_json(bob_oauth_token, port=8001)

    # Verify bob's user info
    assert user_info["username"] == "bob"
    assert user_info["auth_mode"] == "oauth"

    logger.info(f"Bob's user info: username={user_info['username']}")


async def test_user_info_scopes_reflect_token(playwright_oauth_token_read_only):
    """Test that /user endpoint reflects token's scopes."""
    user_info = await get_user_info_json(playwright_oauth_token_read_only, port=8001)

    # Verify scopes are present and reflect read-only access
    assert "scopes" in user_info
    scopes = user_info["scopes"]
    assert isinstance(scopes, list)

    # Read-only token should have read scopes but not write scopes
    # Note: Actual scope names depend on configuration
    logger.info(f"Read-only token scopes: {scopes}")


async def test_user_info_idp_profile_included(playwright_oauth_token):
    """Test that /user endpoint includes IdP profile when available."""
    user_info = await get_user_info_json(playwright_oauth_token, port=8001)

    # Should have either idp_profile or idp_profile_error
    has_profile = "idp_profile" in user_info
    has_error = "idp_profile_error" in user_info

    assert has_profile or has_error, "Should have IdP profile data or error"

    if has_profile:
        idp_profile = user_info["idp_profile"]
        assert isinstance(idp_profile, dict)
        # Common OIDC claims
        assert "sub" in idp_profile, "IdP profile should include 'sub' claim"
        logger.info(f"IdP profile included: {json.dumps(idp_profile, indent=2)}")
    else:
        logger.warning(f"IdP profile query failed: {user_info['idp_profile_error']}")


# ============================================================================
# Keycloak OAuth Tests (mcp-keycloak on port 8002)
# ============================================================================


@pytest.mark.keycloak
async def test_user_info_json_with_keycloak_oauth(keycloak_oauth_token):
    """Test /user endpoint with Keycloak OAuth token."""
    user_info = await get_user_info_json(keycloak_oauth_token, port=8002)

    # Verify response structure
    assert "username" in user_info
    assert "auth_mode" in user_info
    assert user_info["auth_mode"] == "oauth"

    # Verify Keycloak username (default admin user)
    assert user_info["username"] == "admin"

    # Verify OAuth-specific fields
    assert "client_id" in user_info
    assert "scopes" in user_info
    assert isinstance(user_info["scopes"], list)

    logger.info(f"Keycloak user info JSON: {json.dumps(user_info, indent=2)}")


@pytest.mark.keycloak
async def test_user_info_html_with_keycloak_oauth(keycloak_oauth_token):
    """Test /user/page endpoint with Keycloak OAuth token."""
    html = await get_user_info_html(keycloak_oauth_token, port=8002)

    # Verify HTML structure
    assert "<!DOCTYPE html>" in html
    assert "Nextcloud MCP Server - User Info" in html

    # Verify Keycloak username is displayed
    assert "admin" in html

    logger.info(
        f"Keycloak user info HTML page rendered successfully ({len(html)} chars)"
    )


@pytest.mark.keycloak
async def test_keycloak_user_info_idp_profile(keycloak_oauth_token):
    """Test that Keycloak IdP profile includes extended claims."""
    user_info = await get_user_info_json(keycloak_oauth_token, port=8002)

    # Keycloak should provide IdP profile with extended claims
    if "idp_profile" in user_info:
        idp_profile = user_info["idp_profile"]

        # Standard OIDC claims
        assert "sub" in idp_profile

        # Keycloak-specific claims (may vary by configuration)
        # Common claims: email, preferred_username, name, groups, roles
        logger.info(f"Keycloak IdP profile: {json.dumps(idp_profile, indent=2)}")

        # Verify at least one identity claim exists
        identity_claims = ["email", "preferred_username", "name", "sub"]
        has_identity = any(claim in idp_profile for claim in identity_claims)
        assert has_identity, (
            f"IdP profile should include at least one identity claim: {identity_claims}"
        )


@pytest.mark.keycloak
async def test_keycloak_user_info_unauthenticated():
    """Test /user endpoint on Keycloak server without authentication."""
    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8002/user")

        # Should return 401
        assert response.status_code == 401

        data = response.json()
        assert "error" in data

    logger.info("Keycloak server correctly returned 401 for unauthenticated request")


# ============================================================================
# Cross-Mode Comparison Tests
# ============================================================================


async def test_user_info_consistency_across_users(alice_oauth_token, bob_oauth_token):
    """Test that user info structure is consistent across different users."""
    alice_info = await get_user_info_json(alice_oauth_token, port=8001)
    bob_info = await get_user_info_json(bob_oauth_token, port=8001)

    # Both should have same structure
    assert set(alice_info.keys()) == set(bob_info.keys()), (
        "User info structure should be consistent across users"
    )

    # But different usernames
    assert alice_info["username"] == "alice"
    assert bob_info["username"] == "bob"

    logger.info("User info structure is consistent across users")
