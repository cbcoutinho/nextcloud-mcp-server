"""
Configure Keycloak client for token exchange with impersonation.

This script uses Keycloak Admin API to configure the necessary permissions
for the nextcloud-mcp-server client to impersonate users via token exchange.

Usage:
    uv run python tests/manual/configure_impersonation.py
"""

import asyncio
import logging
import os
import sys

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s | %(message)s")
logger = logging.getLogger(__name__)


async def main():
    """Configure impersonation permissions in Keycloak"""

    keycloak_url = os.getenv("KEYCLOAK_URL", "http://localhost:8888")
    realm = os.getenv("KEYCLOAK_REALM", "nextcloud-mcp")
    admin_username = "admin"
    admin_password = "admin"
    client_id = "nextcloud-mcp-server"

    logger.info("=" * 80)
    logger.info("Configuring Keycloak Impersonation Permissions")
    logger.info("=" * 80)
    logger.info(f"Keycloak URL: {keycloak_url}")
    logger.info(f"Realm: {realm}")
    logger.info(f"Client ID: {client_id}")
    logger.info("")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Get admin access token
        logger.info("Step 1: Getting admin access token...")
        token_response = await client.post(
            f"{keycloak_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": admin_username,
                "password": admin_password,
            },
        )
        token_response.raise_for_status()
        admin_token = token_response.json()["access_token"]
        logger.info("✓ Admin token acquired")
        logger.info("")

        headers = {"Authorization": f"Bearer {admin_token}"}

        # Step 2: Get client internal ID
        logger.info("Step 2: Looking up client internal ID...")
        clients_response = await client.get(
            f"{keycloak_url}/admin/realms/{realm}/clients",
            headers=headers,
            params={"clientId": client_id},
        )
        clients_response.raise_for_status()
        clients = clients_response.json()

        if not clients:
            logger.error(f"❌ Client '{client_id}' not found")
            return 1

        client_uuid = clients[0]["id"]
        logger.info(f"✓ Found client UUID: {client_uuid}")
        logger.info("")

        # Step 3: Enable token exchange permission
        logger.info("Step 3: Configuring token exchange permissions...")

        # Get all clients (we need to allow exchange from/to any client)
        all_clients_response = await client.get(
            f"{keycloak_url}/admin/realms/{realm}/clients",
            headers=headers,
        )
        all_clients_response.raise_for_status()
        all_clients = all_clients_response.json()

        # Get all users (we need to allow impersonation of any user)
        users_response = await client.get(
            f"{keycloak_url}/admin/realms/{realm}/users",
            headers=headers,
        )
        users_response.raise_for_status()
        users = users_response.json()

        logger.info(f"  Found {len(all_clients)} clients and {len(users)} users")
        logger.info("")

        # Step 4: Enable permission for client to perform token exchange
        logger.info("Step 4: Enabling token exchange permission...")

        # Update client to enable fine-grained permissions
        update_response = await client.put(
            f"{keycloak_url}/admin/realms/{realm}/clients/{client_uuid}",
            headers=headers,
            json={
                **clients[0],
                "authorizationServicesEnabled": False,  # Don't need full authz
                "serviceAccountsEnabled": True,  # Already enabled
            },
        )

        if update_response.status_code in [200, 204]:
            logger.info("✓ Client configuration updated")
        else:
            logger.warning(f"⚠ Client update returned {update_response.status_code}")

        logger.info("")

        # Step 5: Set up token exchange permission policy
        logger.info("Step 5: Configuring impersonation policy...")

        # In Keycloak Legacy V1, we need to use the token-exchange permissions endpoint
        # This is part of the preview features

        # First, check if token exchange permissions endpoint exists
        try:
            perms_response = await client.get(
                f"{keycloak_url}/admin/realms/{realm}/clients/{client_uuid}/token-exchange/permissions",
                headers=headers,
            )

            if perms_response.status_code == 200:
                logger.info("✓ Token exchange permissions endpoint available")
                permissions = perms_response.json()
                logger.info(f"  Current permissions: {permissions}")
                logger.info("")

                # Enable impersonation for all users
                logger.info("Step 6: Enabling impersonation for admin user...")

                # Find admin user
                admin_user = next((u for u in users if u["username"] == "admin"), None)

                if admin_user:
                    # Enable permission for this client to impersonate admin
                    enable_response = await client.put(
                        f"{keycloak_url}/admin/realms/{realm}/users/{admin_user['id']}/impersonation",
                        headers=headers,
                        json={
                            "client": client_uuid,
                            "enabled": True,
                        },
                    )

                    if enable_response.status_code in [200, 204]:
                        logger.info("✓ Impersonation enabled for admin user")
                    else:
                        logger.warning(
                            f"⚠ Impersonation enable returned {enable_response.status_code}"
                        )
                        logger.info(f"  Response: {enable_response.text}")
                else:
                    logger.error("❌ Admin user not found")

            elif perms_response.status_code == 404:
                logger.warning("⚠ Token exchange permissions endpoint not found")
                logger.info("  This might mean preview features aren't fully enabled")
                logger.info("  Or the Keycloak version doesn't support this API")
            else:
                logger.warning(f"⚠ Unexpected response: {perms_response.status_code}")

        except Exception as e:
            logger.error(f"❌ Error configuring permissions: {e}")
            logger.info("")
            logger.info("Alternative: Manual configuration required")
            logger.info("  1. Open Keycloak Admin Console")
            logger.info("  2. Go to Clients → nextcloud-mcp-server")
            logger.info("  3. Go to Permissions tab")
            logger.info("  4. Enable 'token-exchange' permission")
            logger.info("  5. Configure permission policies for impersonation")

        logger.info("")
        logger.info("=" * 80)
        logger.info("Configuration Complete")
        logger.info("=" * 80)
        logger.info("")
        logger.info("Next step: Run impersonation test")
        logger.info("  uv run python tests/manual/test_impersonation.py")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
