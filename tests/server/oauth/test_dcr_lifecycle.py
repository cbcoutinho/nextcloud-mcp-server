"""
Tests for Dynamic Client Registration (DCR) lifecycle - register and delete.

These tests verify the complete lifecycle of DCR clients:
1. Registration via RFC 7591
2. Token acquisition and use
3. Deletion via RFC 7592
4. Error handling for deletion edge cases

This is critical for ensuring the fixture cleanup code works reliably.
"""

import logging
import os
import secrets
import time
from urllib.parse import quote

import anyio
import httpx
import pytest

from nextcloud_mcp_server.auth.client_registration import delete_client, register_client

from ...conftest import _handle_oauth_consent_screen

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def get_oauth_token_with_client(
    browser,
    client_id: str,
    client_secret: str,
    token_endpoint: str,
    authorization_endpoint: str,
    callback_url: str,
    auth_states: dict,
    scopes: str = "openid profile email notes:read notes:write",
) -> str:
    """
    Helper to obtain OAuth access token using existing client credentials.

    Args:
        browser: Playwright browser instance
        client_id: OAuth client ID
        client_secret: OAuth client secret
        token_endpoint: Token endpoint URL
        authorization_endpoint: Authorization endpoint URL
        callback_url: Callback URL for OAuth redirect
        auth_states: Dict for storing auth codes (from callback server)
        scopes: Space-separated list of scopes to request

    Returns:
        Access token string
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    username = os.getenv("NEXTCLOUD_USERNAME")
    password = os.getenv("NEXTCLOUD_PASSWORD")

    if not all([nextcloud_host, username, password]):
        pytest.skip(
            "OAuth requires NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD"
        )

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

    # Browser automation
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        await page.goto(auth_url, wait_until="networkidle", timeout=60000)
        current_url = page.url

        # Login if needed
        if "/login" in current_url or "/index.php/login" in current_url:
            logger.info("Logging in for DCR lifecycle test...")
            await page.wait_for_selector('input[name="user"]', timeout=10000)
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=60000)

        # Handle consent screen if present
        try:
            await _handle_oauth_consent_screen(page, username)
        except Exception as e:
            logger.debug(f"No consent screen or already authorized: {e}")

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

        logger.info("Successfully obtained access token")
        return access_token


@pytest.mark.integration
async def test_dcr_register_and_delete_lifecycle(
    anyio_backend,
    browser,
    oauth_callback_server,
):
    """
    Test the complete DCR lifecycle: register → use → delete.

    This verifies:
    1. Client registration succeeds
    2. Client can obtain tokens and make API calls
    3. Client deletion succeeds (returns 204)
    4. Deleted client cannot be used again (tokens are revoked)
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Test requires NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    # Discover OIDC endpoints
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        registration_endpoint = oidc_config.get("registration_endpoint")
        token_endpoint = oidc_config.get("token_endpoint")
        authorization_endpoint = oidc_config.get("authorization_endpoint")

    # Step 1: Register client (and capture full response including registration_access_token)
    logger.info("Step 1: Registering OAuth client...")

    # Register manually to capture full response
    client_metadata = {
        "client_name": "DCR Lifecycle Test Client",
        "redirect_uris": [callback_url],
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "openid profile email notes:read",
        "token_type": "Bearer",
    }

    async with httpx.AsyncClient(timeout=30.0) as reg_client:
        reg_response = await reg_client.post(
            registration_endpoint,
            json=client_metadata,
            headers={"Content-Type": "application/json"},
        )
        reg_response.raise_for_status()
        full_client_info = reg_response.json()

    logger.info(f"Full registration response keys: {list(full_client_info.keys())}")
    logger.info(f"Registration response: {full_client_info}")

    # Use the register_client function for the ClientInfo object
    client_info = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name="DCR Lifecycle Test Client 2",
        redirect_uris=[callback_url],
        scopes="openid profile email notes:read",
        token_type="Bearer",
    )

    # Store registration_access_token if present
    registration_access_token = full_client_info.get("registration_access_token")
    logger.info(
        f"Registration access token present: {registration_access_token is not None}"
    )

    logger.info(f"✅ Client registered: {client_info.client_id[:16]}...")

    # Step 2: Obtain token and verify client works
    logger.info("Step 2: Obtaining OAuth token with registered client...")
    access_token = await get_oauth_token_with_client(
        browser=browser,
        client_id=client_info.client_id,
        client_secret=client_info.client_secret,
        token_endpoint=token_endpoint,
        authorization_endpoint=authorization_endpoint,
        callback_url=callback_url,
        auth_states=auth_states,
        scopes="openid profile email notes:read",
    )

    assert access_token, "Failed to obtain access token"
    logger.info(f"✅ Access token obtained: {access_token[:30]}...")

    # Step 3: Delete the client
    logger.info("Step 3: Deleting OAuth client...")
    logger.info(f"Client ID: {client_info.client_id}")
    logger.info(f"Client secret (first 16 chars): {client_info.client_secret[:16]}...")

    # First, let's manually test the deletion endpoint with different auth methods
    deletion_endpoint = f"{nextcloud_host}/apps/oidc/register/{client_info.client_id}"
    logger.info(f"Deletion endpoint: {deletion_endpoint}")

    # Test with both authentication methods
    async with httpx.AsyncClient(timeout=30.0) as test_client:
        # Method 1: HTTP Basic Auth
        logger.info("Method 1: Testing deletion with HTTP Basic Auth...")
        response_basic = await test_client.delete(
            deletion_endpoint,
            auth=(client_info.client_id, client_info.client_secret),
        )
        logger.info(f"HTTP Basic Auth response status: {response_basic.status_code}")
        logger.info(f"Response body: {response_basic.text[:200]}")

        # Method 2: Credentials in JSON body
        logger.info("\nMethod 2: Testing deletion with credentials in JSON body...")
        response_json = await test_client.delete(
            deletion_endpoint,
            json={
                "client_id": client_info.client_id,
                "client_secret": client_info.client_secret,
            },
        )
        logger.info(f"JSON body response status: {response_json.status_code}")
        logger.info(f"Response body: {response_json.text[:200]}")

        # Method 3: Try with query parameters
        logger.info(
            "\nMethod 3: Testing deletion with credentials in query parameters..."
        )
        response_query = await test_client.delete(
            deletion_endpoint,
            params={
                "client_id": client_info.client_id,
                "client_secret": client_info.client_secret,
            },
        )
        logger.info(f"Query params response status: {response_query.status_code}")
        logger.info(f"Response body: {response_query.text[:200]}")

        # Summary
        logger.info("\n=== SUMMARY ===")
        logger.info(f"Basic Auth: {response_basic.status_code}")
        logger.info(f"JSON Body: {response_json.status_code}")
        logger.info(f"Query Params: {response_query.status_code}")

        if response_basic.status_code == 401 and response_json.status_code == 401:
            logger.info("✗ All authentication methods failed with 401 Unauthorized")
        elif (
            response_basic.status_code == 204
            or response_json.status_code == 204
            or response_query.status_code == 204
        ):
            logger.info("✓ At least one authentication method succeeded!")
        else:
            logger.info("Unexpected status codes - need further investigation")

    success = await delete_client(
        nextcloud_url=nextcloud_host,
        client_id=client_info.client_id,
        client_secret=client_info.client_secret,
    )

    assert success, (
        "Client deletion should succeed, but got status from manual test above"
    )
    logger.info(f"✅ Client deleted successfully: {client_info.client_id[:16]}...")

    # Step 4: Verify deleted client cannot obtain new tokens
    logger.info("Step 4: Verifying deleted client cannot obtain new tokens...")

    # Try to use the deleted client to get a token
    # This should fail because the client no longer exists
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

            # If we get here, check the status code
            if token_response.status_code == 401:
                logger.info("✅ Deleted client correctly rejected (401 Unauthorized)")
            else:
                # Unexpected success - client should be deleted
                pytest.fail(
                    f"Deleted client should not be able to obtain tokens, "
                    f"but got status {token_response.status_code}"
                )

        except httpx.HTTPStatusError as e:
            # Expected - client should be rejected
            if e.response.status_code == 401:
                logger.info("✅ Deleted client correctly rejected (401 Unauthorized)")
            else:
                # Re-raise if it's a different error
                raise

    logger.info("✅ Complete DCR lifecycle test passed!")


@pytest.mark.integration
async def test_dcr_delete_with_wrong_credentials(
    anyio_backend,
    oauth_callback_server,
):
    """
    Test that deletion fails with wrong client credentials (401 Unauthorized).

    This verifies:
    1. Client registration succeeds
    2. Deletion with wrong client_secret returns 401
    3. Deletion with correct credentials still works
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Test requires NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    # Discover OIDC endpoints
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        registration_endpoint = oidc_config.get("registration_endpoint")

    # Register client
    logger.info("Registering OAuth client for credential test...")
    client_info = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name="DCR Wrong Credentials Test",
        redirect_uris=[callback_url],
        scopes="openid profile email",
        token_type="Bearer",
    )

    logger.info(f"Client registered: {client_info.client_id[:16]}...")

    # Try to delete with wrong client_secret
    logger.info("Attempting deletion with wrong client_secret...")
    wrong_secret = "wrong_secret_" + secrets.token_urlsafe(32)

    success = await delete_client(
        nextcloud_url=nextcloud_host,
        client_id=client_info.client_id,
        client_secret=wrong_secret,
    )

    assert not success, "Deletion with wrong credentials should fail"
    logger.info("✅ Deletion correctly failed with wrong credentials")

    # Clean up: Delete with correct credentials
    logger.info("Cleaning up: deleting with correct credentials...")
    success = await delete_client(
        nextcloud_url=nextcloud_host,
        client_id=client_info.client_id,
        client_secret=client_info.client_secret,
    )

    assert success, "Deletion with correct credentials should succeed"
    logger.info("✅ Cleanup successful with correct credentials")


@pytest.mark.integration
async def test_dcr_delete_nonexistent_client(
    anyio_backend,
):
    """
    Test that deleting a non-existent client fails gracefully.

    This verifies:
    1. Deletion of fake client_id returns False (not 204)
    2. No exceptions are raised (graceful failure)
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Test requires NEXTCLOUD_HOST")

    # Try to delete a client that doesn't exist
    fake_client_id = "nonexistent_" + secrets.token_urlsafe(16)
    fake_client_secret = secrets.token_urlsafe(32)

    logger.info(f"Attempting to delete non-existent client: {fake_client_id[:16]}...")

    success = await delete_client(
        nextcloud_url=nextcloud_host,
        client_id=fake_client_id,
        client_secret=fake_client_secret,
    )

    assert not success, "Deletion of non-existent client should fail"
    logger.info("✅ Non-existent client deletion correctly failed")


@pytest.mark.integration
async def test_dcr_deletion_is_idempotent(
    anyio_backend,
    oauth_callback_server,
):
    """
    Test that deleting the same client twice fails gracefully on second attempt.

    This verifies:
    1. First deletion succeeds (204)
    2. Second deletion fails gracefully (returns False, not an exception)
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Test requires NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    # Discover OIDC endpoints
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()

        registration_endpoint = oidc_config.get("registration_endpoint")

    # Register client
    logger.info("Registering OAuth client for idempotency test...")
    client_info = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name="DCR Idempotency Test",
        redirect_uris=[callback_url],
        scopes="openid profile email",
        token_type="Bearer",
    )

    logger.info(f"Client registered: {client_info.client_id[:16]}...")

    # First deletion
    logger.info("First deletion attempt...")
    success = await delete_client(
        nextcloud_url=nextcloud_host,
        client_id=client_info.client_id,
        client_secret=client_info.client_secret,
    )

    assert success, "First deletion should succeed"
    logger.info("✅ First deletion succeeded")

    # Second deletion (should fail gracefully)
    logger.info("Second deletion attempt (should fail)...")
    success = await delete_client(
        nextcloud_url=nextcloud_host,
        client_id=client_info.client_id,
        client_secret=client_info.client_secret,
    )

    assert not success, "Second deletion should fail (client already deleted)"
    logger.info("✅ Second deletion correctly failed (client already deleted)")
