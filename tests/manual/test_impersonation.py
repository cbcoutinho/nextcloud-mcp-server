"""
Manual test for RFC 8693 Token Exchange with USER IMPERSONATION.

This script tests whether Keycloak actually supports the requested_subject
parameter for user impersonation, as claimed in ADR-002 to be unsupported.

Test procedure:
1. Get service account token (client_credentials grant)
2. Attempt to exchange token WITH requested_subject parameter
3. Observe actual behavior (success or error)
4. Decode resulting token to verify sub claim

Usage:
    # Start Keycloak and app containers
    docker compose up -d keycloak app

    # Run the test
    uv run python tests/manual/test_impersonation.py
"""

import asyncio
import base64
import json
import logging
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from nextcloud_mcp_server.auth.keycloak_oauth import KeycloakOAuthClient
from nextcloud_mcp_server.client import NextcloudClient

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(levelname)-8s | %(name)-30s | %(message)s"
)
logger = logging.getLogger(__name__)


def decode_jwt(token: str) -> dict:
    """Decode JWT token payload without verification"""
    try:
        # Split token and get payload (second part)
        parts = token.split(".")
        if len(parts) != 3:
            return {"error": "Invalid JWT format"}

        # Decode payload (add padding if needed)
        payload = parts[1]
        padding = 4 - (len(payload) % 4)
        if padding != 4:
            payload += "=" * padding

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as e:
        return {"error": str(e)}


async def main():
    """Test token exchange with impersonation"""

    # Configuration (matches docker-compose mcp-keycloak service)
    keycloak_url = os.getenv("KEYCLOAK_URL", "http://localhost:8888")
    realm = os.getenv("KEYCLOAK_REALM", "nextcloud-mcp")
    client_id = os.getenv("KEYCLOAK_CLIENT_ID", "nextcloud-mcp-server")
    client_secret = os.getenv(
        "KEYCLOAK_CLIENT_SECRET", "mcp-secret-change-in-production"
    )
    nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")
    redirect_uri = "http://localhost:8002/oauth/callback"
    target_user = "admin"  # User to impersonate

    logger.info("=" * 80)
    logger.info("RFC 8693 Token Exchange IMPERSONATION Test")
    logger.info("=" * 80)
    logger.info(f"Keycloak URL: {keycloak_url}")
    logger.info(f"Realm: {realm}")
    logger.info(f"Client ID: {client_id}")
    logger.info(f"Target User: {target_user}")
    logger.info(f"Nextcloud: {nextcloud_host}")
    logger.info("")
    logger.info("⚠️  This test attempts impersonation to verify ADR-002 claims")
    logger.info("")

    # Step 1: Create Keycloak OAuth client
    logger.info("Step 1: Initializing Keycloak OAuth client...")
    oauth_client = KeycloakOAuthClient(
        keycloak_url=keycloak_url,
        realm=realm,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )

    # Discover endpoints
    await oauth_client.discover()
    logger.info(f"✓ Discovered token endpoint: {oauth_client.token_endpoint}")
    logger.info("")

    # Step 2: Check token exchange support
    logger.info("Step 2: Checking token exchange support...")
    supported = await oauth_client.check_token_exchange_support()

    if not supported:
        logger.error("❌ Token exchange is NOT supported by this Keycloak instance")
        logger.error(
            "   You may need to enable it with: --features=preview --features=token-exchange"
        )
        return 1

    logger.info("✓ Token exchange is supported")
    logger.info("")

    # Step 3: Get service account token
    logger.info("Step 3: Requesting service account token (client_credentials)...")
    try:
        service_token_response = await oauth_client.get_service_account_token(
            scopes=["openid", "profile", "email"]
        )
        service_token = service_token_response["access_token"]
        logger.info("✓ Service account token acquired")

        # Decode and show claims
        service_claims = decode_jwt(service_token)
        logger.info(f"  Subject (sub): {service_claims.get('sub')}")
        logger.info(f"  Preferred username: {service_claims.get('preferred_username')}")
        logger.info(f"  Client ID (azp): {service_claims.get('azp')}")
    except Exception as e:
        logger.error(f"❌ Failed to get service account token: {e}")
        return 1

    logger.info("")

    # Step 4: Attempt token exchange WITH impersonation
    logger.info(
        f"Step 4: Attempting token exchange WITH impersonation (requested_subject={target_user})..."
    )
    logger.info(
        "  🧪 This is the actual test - will Keycloak accept requested_subject?"
    )
    logger.info("")

    try:
        user_token_response = await oauth_client.exchange_token_for_user(
            subject_token=service_token,
            target_user_id=target_user,  # ← THE KEY TEST: Request impersonation
            audience=None,
            scopes=["openid", "profile", "email"],
        )

        user_token = user_token_response["access_token"]
        logger.info("✅ Token exchange with impersonation SUCCEEDED!")
        logger.info("")
        logger.info("📊 Response details:")
        logger.info(
            f"  Issued token type: {user_token_response.get('issued_token_type')}"
        )
        logger.info(f"  Token type: {user_token_response.get('token_type')}")
        logger.info(f"  Expires in: {user_token_response.get('expires_in')}s")
        logger.info("")

        # Decode and analyze the exchanged token
        user_claims = decode_jwt(user_token)
        logger.info("📋 Token claims analysis:")
        logger.info(f"  Subject (sub): {user_claims.get('sub')}")
        logger.info(f"  Preferred username: {user_claims.get('preferred_username')}")
        logger.info(f"  Client ID (azp): {user_claims.get('azp')}")
        logger.info(f"  Audience (aud): {user_claims.get('aud')}")
        logger.info("")

        # Verify if impersonation actually worked
        service_sub = service_claims.get("sub")
        user_sub = user_claims.get("sub")

        if service_sub != user_sub:
            logger.info("✅ IMPERSONATION VERIFIED:")
            logger.info(f"   Original sub: {service_sub}")
            logger.info(f"   New sub:      {user_sub}")
            logger.info("")
            logger.info("   ➡️  The subject claim CHANGED - impersonation worked!")
            impersonation_worked = True
        else:
            logger.warning("⚠️  IMPERSONATION DID NOT OCCUR:")
            logger.warning(f"   Subject unchanged: {user_sub}")
            logger.warning("")
            logger.warning("   ➡️  Token exchange succeeded but sub claim is the same")
            logger.warning(
                "       This is delegation/audience change, not impersonation"
            )
            impersonation_worked = False

    except Exception as e:
        logger.error("❌ Token exchange with impersonation FAILED!")
        logger.error(f"   Error: {e}")
        logger.error("")
        logger.error("📋 Error analysis:")

        # Try to extract detailed error message
        error_str = str(e)
        if "requested_subject" in error_str.lower():
            logger.error(
                "   ➡️  Error mentions 'requested_subject' - parameter not supported"
            )
        elif "impersonation" in error_str.lower():
            logger.error("   ➡️  Error mentions 'impersonation' - feature not enabled")
        elif "permission" in error_str.lower():
            logger.error("   ➡️  Error mentions 'permission' - client lacks permissions")
        else:
            logger.error("   ➡️  Generic error - check Keycloak logs for details")

        logger.error("")
        logger.error("💡 Possible causes:")
        logger.error("   1. Keycloak Standard V2 doesn't support requested_subject")
        logger.error("   2. Requires Legacy V1 with --features=preview")
        logger.error("   3. Client lacks impersonation permissions")
        logger.error("   4. Target user doesn't exist")

        return 1

    logger.info("")

    # Step 5: Test impersonated token with Nextcloud API
    if impersonation_worked:
        logger.info("Step 5: Testing impersonated token with Nextcloud API...")
        try:
            # Create Nextcloud client with exchanged token
            nc_client = NextcloudClient.from_token(
                base_url=nextcloud_host, token=user_token, username=target_user
            )

            # Test API call
            capabilities = await nc_client.capabilities()
            logger.info("✓ Nextcloud API call successful with impersonated token")
            logger.info(f"  Version: {capabilities.get('version', {}).get('string')}")

            await nc_client.close()
        except Exception as e:
            logger.error(f"❌ Nextcloud API call failed: {e}")
            logger.error("   The impersonated token may not be valid for Nextcloud")
            return 1

    logger.info("")
    logger.info("=" * 80)
    logger.info("TEST RESULTS SUMMARY")
    logger.info("=" * 80)

    if impersonation_worked:
        logger.info("✅ IMPERSONATION IS SUPPORTED!")
        logger.info("")
        logger.info("Key findings:")
        logger.info("  • Token exchange with requested_subject WORKS")
        logger.info("  • Subject claim successfully changed")
        logger.info("  • Impersonated token works with Nextcloud APIs")
        logger.info("")
        logger.info("⚠️  ADR-002 DOCUMENTATION IS INCORRECT")
        logger.info("   Current docs claim impersonation doesn't work in Standard V2")
        logger.info("   This test proves it DOES work!")
        logger.info("")
        logger.info("Action items:")
        logger.info("  1. Update ADR-002 to mark Tier 1 as IMPLEMENTED")
        logger.info("  2. Remove 'NOT IMPLEMENTED' warnings from code")
        logger.info("  3. Add automated tests for impersonation")
        logger.info("  4. Update oauth-impersonation-findings.md")
    else:
        logger.info("❌ IMPERSONATION IS NOT SUPPORTED")
        logger.info("")
        logger.info("Key findings:")
        logger.info("  • Token exchange with requested_subject FAILED")
        logger.info("  • Keycloak rejected the parameter")
        logger.info("  • Confirms ADR-002 documentation")
        logger.info("")
        logger.info("✅ ADR-002 DOCUMENTATION IS CORRECT")
        logger.info("   Impersonation requires Keycloak Legacy V1")
        logger.info("")
        logger.info("Action items:")
        logger.info("  1. Add this test as evidence to ADR-002")
        logger.info("  2. Document exact error message")
        logger.info("  3. Add 'Verified by testing' note to docs")

    logger.info("")

    return 0 if impersonation_worked else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
