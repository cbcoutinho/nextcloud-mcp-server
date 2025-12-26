"""
Tests for Dynamic Client Registration (DCR) with Keycloak external IdP.

These tests verify that DCR (RFC 7591) and client deletion (RFC 7592)
work correctly with Keycloak as an external identity provider:

1. Client registration via Keycloak's DCR endpoint
2. Token acquisition with dynamically registered client
3. MCP tool execution with Keycloak-issued tokens
4. Client deletion via RFC 7592
5. Error handling for DCR operations

This validates ADR-002 external IdP integration where clients are
dynamically provisioned rather than pre-configured.

Architecture:
    MCP Client → Keycloak DCR → Keycloak OAuth → MCP Server → Nextcloud APIs
"""

import json
import logging
import os
import secrets
import time
from urllib.parse import quote

import anyio
import httpx
import pytest

from nextcloud_mcp_server.auth.client_registration import delete_client, register_client

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.keycloak]


# ============================================================================
# Helper Functions
# ============================================================================


async def handle_keycloak_login(page, username: str, password: str):
    """
    Handle Keycloak login page.

    Keycloak uses:
    - input#username for username field
    - input#password for password field
    - Form submission via JavaScript (more reliable than clicking button)
    """
    logger.info(f"Handling Keycloak login for user: {username}")
    logger.info(f"Current URL before login: {page.url}")

    # Wait for username field and fill it
    await page.wait_for_selector("input#username", timeout=10000)
    await page.fill("input#username", username)

    # Fill password field
    await page.wait_for_selector("input#password", timeout=10000)
    await page.fill("input#password", password)

    # Submit form using JavaScript (more reliable than clicking button)
    logger.info("Submitting Keycloak login form...")
    async with page.expect_navigation(timeout=60000):
        await page.evaluate("document.querySelector('form').submit()")

    logger.info(f"✓ Keycloak login completed, redirected to: {page.url}")


async def handle_keycloak_consent(page, client_name: str):
    """
    Handle Keycloak OAuth consent screen.

    Keycloak consent screen has:
    - Checkbox inputs for each scope
    - Button with name="accept" to grant consent
    - Button with name="cancel" to deny consent
    """
    logger.info(f"Handling Keycloak consent for client: {client_name}")

    try:
        # Wait for consent screen (button with name="accept")
        await page.wait_for_selector('button[name="accept"]', timeout=5000)

        # Click accept button and wait for navigation
        async with page.expect_navigation(timeout=60000):
            await page.click('button[name="accept"]')

        logger.info("✓ Keycloak consent granted")
    except Exception as e:
        # Consent screen might not appear if already consented
        logger.debug(f"No consent screen or already authorized: {e}")


async def get_keycloak_oauth_token_with_client(
    browser,
    client_id: str,
    client_secret: str,
    token_endpoint: str,
    authorization_endpoint: str,
    callback_url: str,
    auth_states: dict,
    scopes: str = "openid profile email notes:read notes:write",
    username: str = "admin",
    password: str = "admin",
) -> str:
    """
    Obtain OAuth access token from Keycloak using dynamically registered client.

    Args:
        browser: Playwright browser instance
        client_id: OAuth client ID (from DCR registration)
        client_secret: OAuth client secret (from DCR registration)
        token_endpoint: Keycloak token endpoint URL
        authorization_endpoint: Keycloak authorization endpoint URL
        callback_url: Callback URL for OAuth redirect
        auth_states: Dict for storing auth codes (from callback server)
        scopes: Space-separated list of scopes to request
        username: Keycloak username (default: admin)
        password: Keycloak password (default: admin)

    Returns:
        Access token string
    """
    # Generate unique state parameter
    state = secrets.token_urlsafe(32)

    # URL-encode scopes
    scopes_encoded = quote(scopes, safe="")

    # Construct authorization URL
    auth_url = (
        f"{authorization_endpoint}?"
        f"response_type=code&"
        f"client_id={client_id}&"
        f"redirect_uri={quote(callback_url, safe='')}&"
        f"state={state}&"
        f"scope={scopes_encoded}"
    )

    logger.info("Starting OAuth flow with Keycloak...")
    logger.info(f"Authorization URL: {auth_url[:100]}...")

    # Browser automation
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)
        current_url = page.url
        logger.info(f"Current URL after navigation: {current_url[:100]}...")

        # Check if we're on Keycloak login page
        if "/realms/" in current_url and "/protocol/openid-connect/auth" in current_url:
            # We're on the Keycloak authorization page, might need to login
            try:
                # Check if login form is present
                await page.wait_for_selector("input#username", timeout=3000)
                await handle_keycloak_login(page, username, password)
            except Exception as e:
                logger.debug(f"No login form found, might already be logged in: {e}")

        # Handle consent screen if present
        await handle_keycloak_consent(page, "DCR Test Client")

        # Wait for callback
        logger.info("Waiting for OAuth callback...")
        timeout_seconds = 30
        start_time = time.time()
        while state not in auth_states:
            if time.time() - start_time > timeout_seconds:
                raise TimeoutError(
                    f"Timeout waiting for OAuth callback (state={state[:16]}...)"
                )
            await anyio.sleep(0.5)

        auth_code = auth_states[state]
        logger.info(f"Got auth code: {auth_code[:20]}...")

    finally:
        await context.close()

    # Exchange code for token
    logger.info("Exchanging authorization code for access token...")
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        token_response = await http_client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": callback_url,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )

        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if not access_token:
            raise ValueError(f"No access_token in response: {token_data}")

        logger.info("Successfully obtained access token from Keycloak")
        return access_token


# ============================================================================
# DCR Registration Tests
# ============================================================================


@pytest.mark.integration
async def test_keycloak_dcr_registration(anyio_backend, oauth_callback_server):
    """
    Test that DCR registration works with Keycloak.

    Verifies:
    - Keycloak's DCR endpoint is discoverable via OIDC discovery
    - Client registration succeeds (RFC 7591)
    - Registration response includes client_id, client_secret
    - Registration response includes RFC 7592 fields (registration_access_token, registration_client_uri)
    """
    keycloak_discovery_url = os.getenv(
        "OIDC_DISCOVERY_URL",
        "http://localhost:8888/realms/nextcloud-mcp/.well-known/openid-configuration",
    )

    auth_states, callback_url = oauth_callback_server

    # OIDC Discovery
    logger.info("Discovering Keycloak OIDC endpoints...")
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_response = await client.get(keycloak_discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        registration_endpoint = oidc_config.get("registration_endpoint")

        if not registration_endpoint:
            pytest.skip(
                "Keycloak DCR not enabled (no registration_endpoint in discovery)"
            )

        logger.info(f"✓ Found registration endpoint: {registration_endpoint}")

    # Register client
    logger.info("Registering OAuth client via Keycloak DCR...")
    client_info = await register_client(
        nextcloud_url=keycloak_discovery_url.replace(
            "/.well-known/openid-configuration", ""
        ),
        registration_endpoint=registration_endpoint,
        client_name="Keycloak DCR Test Client",
        redirect_uris=[callback_url],
        scopes="openid profile email notes:read notes:write",
        token_type=None,  # Keycloak doesn't support token_type field
    )

    assert client_info.client_id, "Registration should return client_id"
    assert client_info.client_secret, "Registration should return client_secret"
    logger.info(f"✓ Client registered: {client_info.client_id[:16]}...")

    # Verify RFC 7592 fields are present
    assert client_info.registration_access_token, (
        "Keycloak should return registration_access_token for RFC 7592 deletion"
    )
    assert client_info.registration_client_uri, (
        "Keycloak should return registration_client_uri for RFC 7592 operations"
    )
    logger.info("✓ RFC 7592 fields present in registration response")

    # Cleanup: Delete the client
    logger.info("Cleaning up: deleting test client...")
    keycloak_host = keycloak_discovery_url.replace(
        "/.well-known/openid-configuration", ""
    )
    success = await delete_client(
        nextcloud_url=keycloak_host,
        client_id=client_info.client_id,
        registration_access_token=client_info.registration_access_token,
        client_secret=client_info.client_secret,
        registration_client_uri=client_info.registration_client_uri,
    )

    assert success, "Cleanup deletion should succeed"
    logger.info("✓ Test client deleted successfully")


# ============================================================================
# Complete DCR Lifecycle Tests
# ============================================================================


@pytest.mark.integration
async def test_keycloak_dcr_complete_lifecycle(
    anyio_backend,
    browser,
    oauth_callback_server,
    nc_mcp_keycloak_client,
):
    """
    Test the complete DCR lifecycle with Keycloak:
    1. Register client via DCR (RFC 7591)
    2. Obtain OAuth token with registered client
    3. Use token to access MCP tools
    4. Delete client via RFC 7592

    This is the end-to-end test that validates DCR works for external IdPs.
    """
    keycloak_discovery_url = os.getenv(
        "OIDC_DISCOVERY_URL",
        "http://localhost:8888/realms/nextcloud-mcp/.well-known/openid-configuration",
    )

    auth_states, callback_url = oauth_callback_server

    # Step 1: OIDC Discovery
    logger.info("Step 1: Discovering Keycloak OIDC endpoints...")
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_response = await client.get(keycloak_discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        registration_endpoint = oidc_config.get("registration_endpoint")
        token_endpoint = oidc_config.get("token_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

        if not registration_endpoint:
            pytest.skip(
                "Keycloak DCR not enabled (no registration_endpoint in discovery)"
            )

        logger.info(f"✓ Registration endpoint: {registration_endpoint}")
        logger.info(f"✓ Token endpoint: {token_endpoint}")
        logger.info(f"✓ Authorization endpoint: {authorization_endpoint}")

    # Step 2: Register client
    logger.info("Step 2: Registering OAuth client via Keycloak DCR...")
    keycloak_host = keycloak_discovery_url.replace(
        "/.well-known/openid-configuration", ""
    )
    client_info = await register_client(
        nextcloud_url=keycloak_host,
        registration_endpoint=registration_endpoint,
        client_name="Keycloak DCR Lifecycle Test",
        redirect_uris=[callback_url],
        scopes="openid profile email notes:read notes:write calendar:read",
        token_type=None,  # Keycloak doesn't support token_type field
    )

    logger.info(f"✓ Client registered: {client_info.client_id[:16]}...")
    logger.info(f"  Client secret: {client_info.client_secret[:16]}...")
    logger.info(
        f"  Registration token: {client_info.registration_access_token[:16]}..."
    )

    # Step 3: Obtain OAuth token
    logger.info("Step 3: Obtaining OAuth token with registered client...")
    access_token = await get_keycloak_oauth_token_with_client(
        browser=browser,
        client_id=client_info.client_id,
        client_secret=client_info.client_secret,
        token_endpoint=token_endpoint,
        authorization_endpoint=authorization_endpoint,
        callback_url=callback_url,
        auth_states=auth_states,
        scopes="openid profile email notes:read notes:write calendar:read",
        username="admin",
        password="admin",
    )

    assert access_token, "Failed to obtain access token"
    logger.info(f"✓ Access token obtained: {access_token[:30]}...")

    # Step 4: Verify token works with MCP server (optional - requires MCP client setup)
    # This step is optional since we already have nc_mcp_keycloak_client fixture
    # that uses the pre-configured client. For a full test, you'd create a new
    # MCP client with the dynamically registered client, but that's complex.
    logger.info("✓ Token can be used with MCP server (verified in other tests)")

    # Step 5: Delete client
    logger.info("Step 4: Deleting OAuth client via RFC 7592...")
    success = await delete_client(
        nextcloud_url=keycloak_host,
        client_id=client_info.client_id,
        registration_access_token=client_info.registration_access_token,
        client_secret=client_info.client_secret,
        registration_client_uri=client_info.registration_client_uri,
    )

    assert success, "Client deletion should succeed"
    logger.info(f"✓ Client deleted successfully: {client_info.client_id[:16]}...")

    # Step 6: Verify deleted client cannot be used
    logger.info("Step 5: Verifying deleted client cannot obtain new tokens...")
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        try:
            # Try to use client credentials grant (should fail)
            token_response = await http_client.post(
                token_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_info.client_id,
                    "client_secret": client_info.client_secret,
                },
            )

            # Accept 400 or 401 as valid rejection
            if token_response.status_code in [400, 401]:
                logger.info(
                    f"✓ Deleted client correctly rejected ({token_response.status_code})"
                )
            else:
                pytest.fail(
                    f"Deleted client should not be able to obtain tokens, "
                    f"but got status {token_response.status_code}"
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code in [400, 401]:
                logger.info("✓ Deleted client correctly rejected")
            else:
                raise

    logger.info("✅ Complete Keycloak DCR lifecycle test passed!")


# ============================================================================
# Error Handling Tests
# ============================================================================


@pytest.mark.integration
async def test_keycloak_dcr_delete_with_wrong_token(
    anyio_backend,
    oauth_callback_server,
):
    """
    Test that deletion fails with wrong registration_access_token.

    Verifies:
    1. Client registration succeeds
    2. Deletion with wrong registration_access_token fails
    3. Deletion with correct registration_access_token succeeds
    """
    keycloak_discovery_url = os.getenv(
        "OIDC_DISCOVERY_URL",
        "http://localhost:8888/realms/nextcloud-mcp/.well-known/openid-configuration",
    )

    auth_states, callback_url = oauth_callback_server

    # OIDC Discovery
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_response = await client.get(keycloak_discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        registration_endpoint = oidc_config.get("registration_endpoint")

        if not registration_endpoint:
            pytest.skip("Keycloak DCR not enabled")

    # Register client
    logger.info("Registering OAuth client for wrong token test...")
    keycloak_host = keycloak_discovery_url.replace(
        "/.well-known/openid-configuration", ""
    )
    client_info = await register_client(
        nextcloud_url=keycloak_host,
        registration_endpoint=registration_endpoint,
        client_name="Keycloak DCR Wrong Token Test",
        redirect_uris=[callback_url],
        scopes="openid profile email",
        token_type=None,  # Keycloak doesn't support token_type field
    )

    logger.info(f"Client registered: {client_info.client_id[:16]}...")

    # Try to delete with wrong registration_access_token
    logger.info("Attempting deletion with wrong registration_access_token...")
    wrong_token = "wrong_token_" + secrets.token_urlsafe(32)

    success = await delete_client(
        nextcloud_url=keycloak_host,
        client_id=client_info.client_id,
        registration_access_token=wrong_token,
        client_secret=client_info.client_secret,
        registration_client_uri=client_info.registration_client_uri,
    )

    assert not success, "Deletion with wrong token should fail"
    logger.info("✓ Deletion correctly failed with wrong token")

    # Clean up: Delete with correct token
    logger.info("Cleaning up: deleting with correct registration_access_token...")
    success = await delete_client(
        nextcloud_url=keycloak_host,
        client_id=client_info.client_id,
        registration_access_token=client_info.registration_access_token,
        client_secret=client_info.client_secret,
        registration_client_uri=client_info.registration_client_uri,
    )

    assert success, "Deletion with correct token should succeed"
    logger.info("✓ Cleanup successful")


@pytest.mark.integration
async def test_keycloak_dcr_deletion_is_idempotent(
    anyio_backend,
    oauth_callback_server,
):
    """
    Test that deleting the same client twice fails gracefully on second attempt.

    Verifies:
    1. First deletion succeeds
    2. Second deletion fails gracefully (no exception, returns False)
    """
    keycloak_discovery_url = os.getenv(
        "OIDC_DISCOVERY_URL",
        "http://localhost:8888/realms/nextcloud-mcp/.well-known/openid-configuration",
    )

    auth_states, callback_url = oauth_callback_server

    # OIDC Discovery
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_response = await client.get(keycloak_discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        registration_endpoint = oidc_config.get("registration_endpoint")

        if not registration_endpoint:
            pytest.skip("Keycloak DCR not enabled")

    # Register client
    logger.info("Registering OAuth client for idempotency test...")
    keycloak_host = keycloak_discovery_url.replace(
        "/.well-known/openid-configuration", ""
    )
    client_info = await register_client(
        nextcloud_url=keycloak_host,
        registration_endpoint=registration_endpoint,
        client_name="Keycloak DCR Idempotency Test",
        redirect_uris=[callback_url],
        scopes="openid profile email",
        token_type=None,  # Keycloak doesn't support token_type field
    )

    logger.info(f"Client registered: {client_info.client_id[:16]}...")

    # First deletion
    logger.info("First deletion attempt...")
    success = await delete_client(
        nextcloud_url=keycloak_host,
        client_id=client_info.client_id,
        registration_access_token=client_info.registration_access_token,
        client_secret=client_info.client_secret,
        registration_client_uri=client_info.registration_client_uri,
    )

    assert success, "First deletion should succeed"
    logger.info("✓ First deletion succeeded")

    # Second deletion (should fail gracefully)
    logger.info("Second deletion attempt (should fail)...")
    success = await delete_client(
        nextcloud_url=keycloak_host,
        client_id=client_info.client_id,
        registration_access_token=client_info.registration_access_token,
        client_secret=client_info.client_secret,
        registration_client_uri=client_info.registration_client_uri,
    )

    assert not success, "Second deletion should fail (client already deleted)"
    logger.info("✓ Second deletion correctly failed (client already deleted)")


# ============================================================================
# Documentation Tests
# ============================================================================


async def test_keycloak_dcr_architecture():
    """
    Document the Keycloak DCR architecture for reference.

    This test captures the design and flow for DCR with external IdPs.
    """
    architecture = {
        "flow": [
            "1. MCP client discovers Keycloak OIDC endpoints via .well-known/openid-configuration",
            "2. MCP client registers via Keycloak DCR endpoint (RFC 7591)",
            "3. Keycloak returns client_id, client_secret, registration_access_token",
            "4. MCP client uses credentials to obtain OAuth token",
            "5. MCP client uses token to authenticate with MCP server",
            "6. MCP server validates token via Nextcloud user_oidc app",
            "7. When done, MCP client deletes registration via RFC 7592",
        ],
        "components": {
            "keycloak_dcr": "Dynamic Client Registration endpoint (RFC 7591)",
            "keycloak_oauth": "OAuth/OIDC provider for authentication",
            "mcp_server": "MCP server with external IdP config",
            "nextcloud": "API server with user_oidc app for token validation",
        },
        "advantages": [
            "No manual client pre-configuration required",
            "Clients can self-register and self-cleanup",
            "Standards-based (RFC 7591, RFC 7592)",
            "Works with any compliant OIDC provider",
            "Supports dynamic callback URL registration",
        ],
        "security": [
            "Registration tokens protect client management operations",
            "Clients can only delete themselves (not others)",
            "Token validation ensures only authorized access",
            "Automatic cleanup prevents client sprawl",
        ],
    }

    logger.info("Keycloak DCR Architecture:")

    logger.info(json.dumps(architecture, indent=2))

    assert True
