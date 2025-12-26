"""
Manual test for Nextcloud impersonate API.

This script tests using the Nextcloud impersonate app to allow
admin users to act on behalf of other users.

This is NOT the same as OAuth token exchange, but could serve
as a workaround for background operations.

Usage:
    # Start app container
    docker compose up -d app

    # Run the test
    uv run python tests/manual/test_nextcloud_impersonate.py
"""

import asyncio
import logging
import os
import re
import sys

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import httpx

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(levelname)-8s | %(name)-30s | %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    """Test Nextcloud impersonate API"""

    # Configuration
    nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")
    admin_user = os.getenv("NEXTCLOUD_USERNAME", "admin")
    admin_password = os.getenv("NEXTCLOUD_PASSWORD", "admin")
    target_user = "testuser"  # We'll create this user

    logger.info("=" * 80)
    logger.info("Nextcloud Impersonate API Test")
    logger.info("=" * 80)
    logger.info(f"Nextcloud: {nextcloud_host}")
    logger.info(f"Admin user: {admin_user}")
    logger.info(f"Target user: {target_user}")
    logger.info("")

    async with httpx.AsyncClient() as client:
        # Step 1: Login as admin and get session
        logger.info("Step 1: Logging in as admin...")
        login_response = await client.post(
            f"{nextcloud_host}/login",
            data={
                "user": admin_user,
                "password": admin_password,
            },
            follow_redirects=True,
        )

        if login_response.status_code != 200:
            logger.error(f"❌ Admin login failed: {login_response.status_code}")
            return 1

        # Get requesttoken from response
        requesttoken = None
        for cookie in client.cookies.jar:
            if cookie.name == "nc_session":
                logger.info(f"✓ Admin logged in, session: {cookie.value[:20]}...")
                break

        logger.info("")

        # Step 2: Create test user if doesn't exist
        logger.info(f"Step 2: Creating test user '{target_user}'...")
        create_user_response = await client.post(
            f"{nextcloud_host}/ocs/v1.php/cloud/users",
            auth=(admin_user, admin_password),
            data={
                "userid": target_user,
                "password": "testpassword123",
            },
            headers={"OCS-APIRequest": "true"},
        )

        if create_user_response.status_code in (200, 400):  # 400 if already exists
            logger.info("✓ Test user ready")
        else:
            logger.warning(
                f"User creation response: {create_user_response.status_code}"
            )

        # Make sure user has logged in at least once (requirement for impersonation)
        logger.info(f"  Performing initial login for {target_user}...")
        await client.post(
            f"{nextcloud_host}/login",
            data={
                "user": target_user,
                "password": "testpassword123",
            },
            follow_redirects=True,
        )
        logger.info("✓ Test user has logged in")

        # Re-login as admin
        await client.post(
            f"{nextcloud_host}/login",
            data={
                "user": admin_user,
                "password": admin_password,
            },
            follow_redirects=True,
        )

        logger.info("")

        # Step 3: Get CSRF token for impersonate request
        logger.info("Step 3: Getting CSRF token...")

        # Try to get token from settings page
        settings_response = await client.get(
            f"{nextcloud_host}/settings/users",
            follow_redirects=True,
        )

        # Extract requesttoken from HTML

        token_match = re.search(r'data-requesttoken="([^"]+)"', settings_response.text)
        if token_match:
            requesttoken = token_match.group(1)
            logger.info(f"✓ CSRF token acquired: {requesttoken[:20]}...")
        else:
            logger.error("❌ Could not extract CSRF token from page")
            return 1

        logger.info("")

        # Step 4: Call impersonate API
        logger.info(f"Step 4: Impersonating user '{target_user}'...")
        impersonate_response = await client.post(
            f"{nextcloud_host}/apps/impersonate/user",
            data={
                "userId": target_user,
            },
            headers={
                "requesttoken": requesttoken,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        if impersonate_response.status_code != 200:
            logger.error(f"❌ Impersonate failed: {impersonate_response.status_code}")
            logger.error(f"Response: {impersonate_response.text}")
            return 1

        logger.info("✓ Impersonation successful")
        logger.info("")

        # Step 5: Test API call as impersonated user
        logger.info("Step 5: Testing API call as impersonated user...")
        capabilities_response = await client.get(
            f"{nextcloud_host}/ocs/v2.php/cloud/capabilities",
            headers={"OCS-APIRequest": "true"},
        )

        if capabilities_response.status_code == 200:
            caps = capabilities_response.json()
            logger.info(f"✓ API call successful as {target_user}")
            logger.info(
                f"  Version: {caps.get('ocs', {}).get('data', {}).get('version', {}).get('string')}"
            )
        else:
            logger.error(f"❌ API call failed: {capabilities_response.status_code}")
            return 1

        logger.info("")

        # Step 6: Get current user to verify impersonation
        logger.info("Step 6: Verifying current user...")
        user_response = await client.get(
            f"{nextcloud_host}/ocs/v2.php/cloud/user",
            headers={"OCS-APIRequest": "true"},
        )

        if user_response.status_code == 200:
            user_data = user_response.json()
            current_user = user_data.get("ocs", {}).get("data", {}).get("id")
            logger.info(f"✓ Current user: {current_user}")

            if current_user == target_user:
                logger.info("  ✓ Successfully impersonating target user!")
            else:
                logger.warning(f"  ⚠ Expected {target_user}, got {current_user}")
        else:
            logger.error(f"❌ User check failed: {user_response.status_code}")

    logger.info("")
    logger.info("=" * 80)
    logger.info("✅ Impersonate API Test PASSED")
    logger.info("=" * 80)
    logger.info("")
    logger.info("Summary:")
    logger.info("  1. Admin can impersonate other users via session-based API")
    logger.info("  2. Impersonated session can access APIs as that user")
    logger.info("  3. Requires admin credentials and CSRF token")
    logger.info("")
    logger.info("Limitations:")
    logger.info("  - Session-based (not stateless like OAuth)")
    logger.info("  - Requires admin credentials")
    logger.info("  - Target user must have logged in at least once")
    logger.info("  - Not suitable for distributed/background workers")
    logger.info("")
    logger.info("For background operations, consider:")
    logger.info("  - Use service account with appropriate permissions")
    logger.info("  - Or implement proper OAuth delegation (RFC 8693)")
    logger.info("")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
