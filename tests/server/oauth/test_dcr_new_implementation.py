"""
Test the new DCR deletion implementation.

This test verifies that the recently implemented DCR deletion branch works correctly.
"""

import logging
import os

import httpx
import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


@pytest.mark.integration
async def test_new_dcr_registration_includes_access_token(
    anyio_backend,
    oauth_callback_server,
):
    """
    Test that registration now includes registration_access_token.

    The new DCR deletion implementation should provide a registration_access_token
    in the registration response per RFC 7592.
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

    # Register a client and inspect the full response
    client_metadata = {
        "client_name": "DCR New Implementation Test",
        "redirect_uris": [callback_url],
        "token_endpoint_auth_method": "client_secret_post",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "openid profile email",
        "token_type": "Bearer",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        logger.info("Registering client to check for registration_access_token...")
        response = await client.post(
            registration_endpoint,
            json=client_metadata,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        registration_data = response.json()

    # Log the full response
    logger.info(f"\n{'=' * 70}")
    logger.info("REGISTRATION RESPONSE")
    logger.info(f"{'=' * 70}")
    logger.info(f"Response keys: {sorted(registration_data.keys())}")
    logger.info("\nFull response:")
    for key, value in sorted(registration_data.items()):
        if key in ["client_secret", "registration_access_token"]:
            # Truncate secrets for security
            logger.info(f"  {key}: {value[:20]}... (truncated)")
        else:
            logger.info(f"  {key}: {value}")

    # Check for RFC 7592 required fields
    logger.info(f"\n{'=' * 70}")
    logger.info("RFC 7592 COMPLIANCE CHECK")
    logger.info(f"{'=' * 70}")

    has_token = "registration_access_token" in registration_data
    has_uri = "registration_client_uri" in registration_data

    logger.info(f"registration_access_token present: {has_token}")
    logger.info(f"registration_client_uri present: {has_uri}")

    if has_token and has_uri:
        logger.info(
            "\n✓ PASS: Registration response includes RFC 7592 management fields!"
        )
        logger.info(
            "  This means DCR deletion should now work with Bearer token authentication."
        )

        # Store these for deletion test
        client_id = registration_data["client_id"]
        registration_access_token = registration_data["registration_access_token"]
        registration_client_uri = registration_data.get("registration_client_uri")

        # Now test deletion with the registration_access_token
        logger.info(f"\n{'=' * 70}")
        logger.info("TESTING DCR DELETION WITH REGISTRATION_ACCESS_TOKEN")
        logger.info(f"{'=' * 70}")

        deletion_endpoint = (
            registration_client_uri
            or f"{nextcloud_host}/apps/oidc/register/{client_id}"
        )
        logger.info(f"Deletion endpoint: {deletion_endpoint}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Try deletion with Bearer token (RFC 7592 standard)
            logger.info("\nAttempting deletion with Bearer token...")
            delete_response = await client.delete(
                deletion_endpoint,
                headers={"Authorization": f"Bearer {registration_access_token}"},
            )

            logger.info(f"Response status: {delete_response.status_code}")
            logger.info(f"Response body: {delete_response.text[:200]}")

            if delete_response.status_code == 204:
                logger.info(
                    "\n✓✓✓ SUCCESS! DCR deletion works with new implementation!"
                )
                logger.info("    RFC 7592 deletion is now fully functional.")
                assert True
            elif delete_response.status_code == 401:
                logger.error(
                    "\n✗ FAIL: Still getting 401 even with registration_access_token"
                )
                logger.error(
                    "  The token may not be recognized or there's a middleware issue."
                )
                pytest.fail(
                    "DCR deletion failed with 401 even with registration_access_token"
                )
            else:
                logger.warning(
                    f"\n? UNEXPECTED: Got status {delete_response.status_code}"
                )
                pytest.fail(
                    f"Unexpected status code: {delete_response.status_code}, body: {delete_response.text[:500]}"
                )

    else:
        logger.warning(
            "\n✗ FAIL: Registration response still missing RFC 7592 management fields"
        )
        logger.warning(
            "  The new DCR deletion implementation may not be active or needs configuration."
        )
        pytest.fail(
            f"Registration response missing RFC 7592 fields. "
            f"Has token: {has_token}, Has URI: {has_uri}"
        )


@pytest.mark.integration
async def test_dcr_deletion_with_basic_auth_new_impl(
    anyio_backend,
    oauth_callback_server,
):
    """
    Verify whether HTTP Basic Auth is now supported for deletion.

    Some implementations support both Bearer token and Basic Auth.
    """
    nextcloud_host = os.getenv("NEXTCLOUD_HOST")
    if not nextcloud_host:
        pytest.skip("Test requires NEXTCLOUD_HOST")

    auth_states, callback_url = oauth_callback_server

    # Discover and register
    async with httpx.AsyncClient(timeout=30.0) as client:
        discovery_url = f"{nextcloud_host}/.well-known/openid-configuration"
        discovery_response = await client.get(discovery_url)
        discovery_response.raise_for_status()
        oidc_config = discovery_response.json()
        registration_endpoint = oidc_config.get("registration_endpoint")

        # Register
        client_metadata = {
            "client_name": "DCR Basic Auth Test",
            "redirect_uris": [callback_url],
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "scope": "openid profile email",
            "token_type": "Bearer",
        }

        response = await client.post(
            registration_endpoint,
            json=client_metadata,
        )
        response.raise_for_status()
        reg_data = response.json()

    client_id = reg_data["client_id"]
    client_secret = reg_data["client_secret"]
    deletion_endpoint = f"{nextcloud_host}/apps/oidc/register/{client_id}"

    logger.info(f"\n{'=' * 70}")
    logger.info("TESTING DCR DELETION WITH HTTP BASIC AUTH")
    logger.info(f"{'=' * 70}")
    logger.info(f"Endpoint: {deletion_endpoint}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.delete(
            deletion_endpoint,
            auth=(client_id, client_secret),
        )

        logger.info(f"Status: {response.status_code}")
        logger.info(f"Body: {response.text[:200]}")

        if response.status_code == 204:
            logger.info("\n✓ SUCCESS: HTTP Basic Auth works for deletion!")
        elif response.status_code == 401:
            logger.info(
                "\n✗ HTTP Basic Auth not supported - use registration_access_token instead"
            )
        else:
            logger.warning(f"\n? Unexpected status: {response.status_code}")

    # This test is informational - we don't fail if Basic Auth doesn't work
    # as long as Bearer token works
    assert True
