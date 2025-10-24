"""
Test DCR deletion endpoint with different authentication methods.

This simplified test focuses only on testing the deletion endpoint
with various authentication methods to answer the question:
"Does the 401 issue occur for both basic auth and credentials in the body?"
"""

import logging
import os

import httpx
import pytest

from nextcloud_mcp_server.auth.client_registration import register_client

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


@pytest.mark.integration
async def test_dcr_deletion_authentication_methods(
    anyio_backend,
    oauth_callback_server,
):
    """
    Test DCR deletion with different authentication methods.

    Tests:
    1. HTTP Basic Auth (client_id:client_secret)
    2. Credentials in JSON body
    3. Credentials in query parameters

    This answers: Does the 401 issue occur with all authentication methods?
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

    # Register a client for testing
    logger.info("Registering test client...")
    client_info = await register_client(
        nextcloud_url=nextcloud_host,
        registration_endpoint=registration_endpoint,
        client_name="DCR Auth Methods Test",
        redirect_uris=[callback_url],
        scopes="openid profile email",
        token_type="Bearer",
    )

    deletion_endpoint = f"{nextcloud_host}/apps/oidc/register/{client_info.client_id}"
    logger.info(f"\nTesting deletion endpoint: {deletion_endpoint}")
    logger.info(f"Client ID: {client_info.client_id}")
    logger.info(f"Client Secret (first 16 chars): {client_info.client_secret[:16]}...")

    results = {}

    async with httpx.AsyncClient(timeout=30.0) as test_client:
        # Method 1: HTTP Basic Auth
        logger.info("\n=== Method 1: HTTP Basic Auth ===")
        try:
            response = await test_client.delete(
                deletion_endpoint,
                auth=(client_info.client_id, client_info.client_secret),
            )
            results["basic_auth"] = {
                "status": response.status_code,
                "body": response.text[:200],
            }
            logger.info(f"Status: {response.status_code}")
            logger.info(f"Body: {response.text[:200]}")
        except Exception as e:
            results["basic_auth"] = {"status": "error", "error": str(e)}
            logger.error(f"Error: {e}")

        # Method 2: Credentials in JSON body
        logger.info("\n=== Method 2: Credentials in JSON Body ===")
        try:
            response = await test_client.delete(
                deletion_endpoint,
                json={
                    "client_id": client_info.client_id,
                    "client_secret": client_info.client_secret,
                },
            )
            results["json_body"] = {
                "status": response.status_code,
                "body": response.text[:200],
            }
            logger.info(f"Status: {response.status_code}")
            logger.info(f"Body: {response.text[:200]}")
        except Exception as e:
            results["json_body"] = {"status": "error", "error": str(e)}
            logger.error(f"Error: {e}")

        # Method 3: Credentials in query parameters
        logger.info("\n=== Method 3: Credentials in Query Parameters ===")
        try:
            response = await test_client.delete(
                deletion_endpoint,
                params={
                    "client_id": client_info.client_id,
                    "client_secret": client_info.client_secret,
                },
            )
            results["query_params"] = {
                "status": response.status_code,
                "body": response.text[:200],
            }
            logger.info(f"Status: {response.status_code}")
            logger.info(f"Body: {response.text[:200]}")
        except Exception as e:
            results["query_params"] = {"status": "error", "error": str(e)}
            logger.error(f"Error: {e}")

        # Method 4: No authentication (baseline)
        logger.info("\n=== Method 4: No Authentication (Baseline) ===")
        try:
            response = await test_client.delete(deletion_endpoint)
            results["no_auth"] = {
                "status": response.status_code,
                "body": response.text[:200],
            }
            logger.info(f"Status: {response.status_code}")
            logger.info(f"Body: {response.text[:200]}")
        except Exception as e:
            results["no_auth"] = {"status": "error", "error": str(e)}
            logger.error(f"Error: {e}")

    # Print summary
    logger.info("\n" + "=" * 70)
    logger.info("SUMMARY: DCR Deletion Authentication Methods")
    logger.info("=" * 70)

    for method, result in results.items():
        status = result.get("status", "unknown")
        logger.info(f"{method:20s} → Status: {status}")

    # Analysis
    logger.info("\n" + "=" * 70)
    logger.info("ANALYSIS")
    logger.info("=" * 70)

    all_401 = all(
        r.get("status") == 401 for r in results.values() if r.get("status") != "error"
    )
    any_204 = any(r.get("status") == 204 for r in results.values())

    if all_401:
        logger.info("✗ ALL authentication methods return 401 Unauthorized")
        logger.info(
            "  This indicates the deletion endpoint does not accept any form of credentials."
        )
        logger.info(
            "  Likely cause: RFC 7592 not fully implemented (missing registration_access_token)"
        )
    elif any_204:
        logger.info("✓ At least one authentication method succeeded (204 No Content)")
        for method, result in results.items():
            if result.get("status") == 204:
                logger.info(f"  Working method: {method}")
    else:
        logger.info("? Mixed results - further investigation needed")
        for method, result in results.items():
            logger.info(f"  {method}: {result.get('status')}")

    # Document the finding
    assert all_401 or any_204, (
        f"Expected either all 401s (not implemented) or at least one 204 (working). "
        f"Got: {results}"
    )

    if all_401:
        logger.info(
            "\n✓ Test confirms: DCR deletion returns 401 with ALL authentication methods"
        )
    else:
        logger.info("\n✓ Test confirms: DCR deletion works with at least one method")
