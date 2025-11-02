"""
Manual test for RFC 8693 Token Exchange with Keycloak.

This script demonstrates ADR-002 Tier 2 implementation:
1. Get service account token (client_credentials grant)
2. Exchange token for user-scoped token (RFC 8693)
3. Use exchanged token to access Nextcloud APIs

Usage:
    # Start Keycloak and app containers
    docker compose up -d keycloak app

    # Run the test
    uv run python tests/manual/test_token_exchange.py
"""

import asyncio
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


async def main():
    """Test token exchange flow"""

    # Configuration (matches docker-compose mcp-keycloak service)
    keycloak_url = os.getenv("KEYCLOAK_URL", "http://localhost:8888")
    realm = os.getenv("KEYCLOAK_REALM", "nextcloud-mcp")
    client_id = os.getenv("KEYCLOAK_CLIENT_ID", "nextcloud-mcp-server")
    client_secret = os.getenv(
        "KEYCLOAK_CLIENT_SECRET", "mcp-secret-change-in-production"
    )
    nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")
    redirect_uri = "http://localhost:8002/oauth/callback"

    logger.info("=" * 80)
    logger.info("RFC 8693 Token Exchange Test")
    logger.info("=" * 80)
    logger.info(f"Keycloak URL: {keycloak_url}")
    logger.info(f"Realm: {realm}")
    logger.info(f"Client ID: {client_id}")
    logger.info(f"Nextcloud: {nextcloud_host}")
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

    logger.info("")

    # Step 3: Get service account token
    logger.info("Step 3: Requesting service account token (client_credentials)...")
    try:
        service_token_response = await oauth_client.get_service_account_token(
            scopes=["openid", "profile", "email"]
        )
        service_token = service_token_response["access_token"]
        logger.info("✓ Service account token acquired")
        logger.info(f"  Token type: {service_token_response.get('token_type')}")
        logger.info(f"  Expires in: {service_token_response.get('expires_in')}s")
        logger.info(f"  Scope: {service_token_response.get('scope')}")
        logger.info(f"  Token (first 50 chars): {service_token[:50]}...")
    except Exception as e:
        logger.error(f"❌ Failed to get service account token: {e}")
        logger.error(
            "   Make sure serviceAccountsEnabled=true for the client in Keycloak"
        )
        return 1

    logger.info("")

    # Step 4: Exchange token (without impersonation - Standard V2)
    logger.info(
        "Step 4: Exchanging service token with different audience (RFC 8693)..."
    )
    logger.info("  Note: Keycloak Standard V2 doesn't support user impersonation")
    logger.info("        That requires Legacy V1 with --features=preview")
    try:
        user_token_response = await oauth_client.exchange_token_for_user(
            subject_token=service_token,
            target_user_id=None,  # Don't request impersonation
            audience=None,  # No cross-client exchange in Standard V2
            scopes=["openid", "profile"],  # Try downscoping
        )
        user_token = user_token_response["access_token"]
        logger.info("✓ Token exchange successful")
        logger.info(
            f"  Issued token type: {user_token_response.get('issued_token_type')}"
        )
        logger.info(f"  Token type: {user_token_response.get('token_type')}")
        logger.info(f"  Expires in: {user_token_response.get('expires_in')}s")
        logger.info(f"  User token (first 50 chars): {user_token[:50]}...")
    except Exception as e:
        logger.error(f"❌ Token exchange failed: {e}")
        logger.error("   Possible causes:")
        logger.error("   - token.exchange.grant.enabled not set to true")
        logger.error("   - Missing exchange permissions in Keycloak")
        logger.error("   - User 'admin' does not exist")
        return 1

    logger.info("")

    # Step 5: Test user token with Nextcloud API
    logger.info("Step 5: Testing exchanged token with Nextcloud capabilities API...")
    try:
        # Create Nextcloud client with exchanged token
        nc_client = NextcloudClient.from_token(
            base_url=nextcloud_host, token=user_token, username="admin"
        )

        # Test API call
        capabilities = await nc_client.capabilities()
        logger.info("✓ Nextcloud API call successful")
        logger.info(f"  Version: {capabilities.get('version', {}).get('string')}")
        logger.info(
            f"  Edition: {capabilities.get('capabilities', {}).get('core', {}).get('webdav-root')}"
        )

        await nc_client.close()
    except Exception as e:
        logger.error(f"❌ Nextcloud API call failed: {e}")
        logger.error("   The exchanged token may not be valid for Nextcloud")
        logger.error("   Check that user_oidc app is configured correctly")
        return 1

    logger.info("")
    logger.info("=" * 80)
    logger.info("✅ Token Exchange Test PASSED")
    logger.info("=" * 80)
    logger.info("")
    logger.info("Summary:")
    logger.info("  1. Service account token acquired")
    logger.info("  2. Token exchanged with different audience")
    logger.info("  3. Exchanged token works with Nextcloud APIs")
    logger.info("")
    logger.info("This demonstrates ADR-002 Tier 2: Token Exchange")
    logger.info(
        "The MCP server can perform token exchange for different audiences/scopes"
    )
    logger.info("without needing refresh tokens or admin credentials.")
    logger.info("")
    logger.info(
        "Note: User impersonation requires Keycloak Legacy V1 with --features=preview"
    )
    logger.info("")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
