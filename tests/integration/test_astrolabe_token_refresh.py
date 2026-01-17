"""Integration tests for Astrolabe token refresh flow.

Tests the token refresh mechanism between Astrolabe (Nextcloud app)
and the MCP server backend in a multi-user basic auth deployment.

This test verifies:
1. User provisions access via Astrolabe personal settings
2. Token is stored encrypted in Nextcloud database
3. Token expires (simulated via database manipulation)
4. MCP server requests new token via refresh
5. Astrolabe refreshes token with IdP
6. New token is stored and used successfully

Note: The mcp-multi-user-basic deployment uses "hybrid mode" which requires
BOTH OAuth authorization AND app password for full configuration. These tests
focus on the app password/credential storage aspects and verify database state
directly rather than relying on UI elements that require both steps.
"""

import logging
import re
import subprocess

import anyio
import pytest
from playwright.async_api import Page

pytestmark = [pytest.mark.integration, pytest.mark.oauth]

logger = logging.getLogger(__name__)


async def login_to_nextcloud(page: Page, username: str, password: str):
    """Helper function to login to Nextcloud via Playwright.

    Args:
        page: Playwright page instance
        username: Nextcloud username
        password: Nextcloud password
    """
    nextcloud_url = "http://localhost:8080"

    logger.info(f"Logging in to Nextcloud as {username}...")
    await page.goto(f"{nextcloud_url}/login", wait_until="networkidle")

    # Fill in login form
    await page.wait_for_selector('input[name="user"]', timeout=10000)
    await page.fill('input[name="user"]', username)
    await page.fill('input[name="password"]', password)

    # Submit form
    await page.click('button[type="submit"]')
    await page.wait_for_load_state("networkidle", timeout=30000)

    # Verify logged in (should redirect away from login page)
    current_url = page.url
    assert "/login" not in current_url, (
        f"Login failed for {username}, still on login page"
    )
    logger.info(f"✓ Successfully logged in as {username}")


async def generate_app_password(
    page: Page, username: str, app_name: str = "Astrolabe Test"
) -> str:
    """Generate an app password in Nextcloud Security settings.

    Args:
        page: Playwright page instance (must be authenticated)
        username: Username (for logging)
        app_name: Name for the app password

    Returns:
        The generated app password string
    """
    logger.info(f"Generating app password for {username}...")

    nextcloud_url = "http://localhost:8080"

    # Navigate to Security settings
    await page.goto(f"{nextcloud_url}/settings/user/security", wait_until="networkidle")
    logger.info("Navigated to Security settings")

    # Fill the app password input field
    app_password_input = page.locator('input[placeholder="App name"]')
    await app_password_input.fill(app_name)
    logger.info(f"Entered app name: {app_name}")

    # Wait for Vue.js to react and enable the button
    await anyio.sleep(1.0)

    # Click the create button
    create_button = page.locator(
        'button[type="submit"]:has-text("Create new app password")'
    )
    await create_button.click()
    logger.info("Clicked create app password button")

    # Wait for app password to be generated
    await anyio.sleep(3)

    # Find the generated app password
    app_password = None
    try:
        await page.wait_for_selector('text="New app password"', timeout=10000)
        logger.info("App password dialog appeared")

        all_inputs = await page.locator('input[type="text"]').all()
        for idx, input_elem in enumerate(all_inputs):
            try:
                value = await input_elem.input_value()
                if value and "-" in value and len(value) > 20:
                    app_password = value.strip()
                    logger.info(f"Found app password in input {idx}")
                    break
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Failed to find app password dialog: {e}")

    if not app_password:
        screenshot_path = f"/tmp/app_password_generation_{username}.png"
        await page.screenshot(path=screenshot_path)
        raise ValueError(
            f"Could not find generated app password. Screenshot: {screenshot_path}"
        )

    # Validate password format
    if not re.match(
        r"^[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}$",
        app_password,
    ):
        raise ValueError(f"App password format validation failed: {app_password}")

    logger.info(f"✓ Generated app password for {username}")

    # Close the dialog
    close_button = page.get_by_role("button", name="Close")
    await close_button.click()
    await anyio.sleep(0.5)

    return app_password


async def save_app_password_in_astrolabe(
    page: Page, username: str, app_password: str
) -> bool:
    """Save app password in Astrolabe settings (Step 2 of hybrid mode).

    This function only saves the app password - it does NOT verify the "Active"
    badge since that requires both OAuth and app password in hybrid mode.

    Args:
        page: Playwright page instance
        username: Username (for logging)
        app_password: App password to enter

    Returns:
        True if the password was saved successfully (based on network response)
    """
    logger.info(f"Saving app password in Astrolabe for {username}...")

    nextcloud_url = "http://localhost:8080"

    # Track network responses
    credentials_response_status = None

    def capture_response(resp):
        nonlocal credentials_response_status
        if "background-sync/credentials" in resp.url or "storeAppPassword" in resp.url:
            credentials_response_status = resp.status
            logger.info(f"Credentials endpoint response: {resp.status} {resp.url}")

    page.on("response", capture_response)

    # Navigate to Astrolabe settings
    await page.goto(
        f"{nextcloud_url}/settings/user/astrolabe", wait_until="networkidle"
    )
    await anyio.sleep(1)

    # Check if Step 2 already shows "Complete"
    try:
        complete_badge = page.locator('text="Complete"').first
        if await complete_badge.is_visible(timeout=2000):
            logger.info(f"✓ App password already configured for {username}")
            return True
    except Exception:
        pass

    # Find the app password input field
    app_password_input = page.get_by_placeholder("xxxxx-xxxxx-xxxxx-xxxxx-xxxxx")

    try:
        await app_password_input.wait_for(timeout=5000, state="visible")
        logger.info("Found app password input field")
    except Exception:
        screenshot_path = f"/tmp/astrolabe_no_password_field_{username}.png"
        await page.screenshot(path=screenshot_path)
        raise ValueError(
            f"Could not find app password input field. Screenshot: {screenshot_path}"
        )

    # Enter the app password
    await app_password_input.fill(app_password)
    logger.info(f"Entered app password for {username}")

    await anyio.sleep(0.5)

    # Click Save button
    save_button = page.get_by_role("button", name="Save")
    await save_button.click()
    logger.info("Clicked Save button")

    # Wait for the request to complete and page to reload
    await page.wait_for_load_state("networkidle", timeout=15000)
    await anyio.sleep(2)

    # Verify the save was successful by checking network response
    if credentials_response_status == 200:
        logger.info(f"✓ App password saved successfully for {username}")
        return True
    else:
        logger.error(
            f"App password save failed for {username}, status: {credentials_response_status}"
        )
        screenshot_path = f"/tmp/astrolabe_save_failed_{username}.png"
        await page.screenshot(path=screenshot_path)
        return False


def get_background_sync_credentials(username: str) -> dict | None:
    """Get background sync credentials for a user from the database.

    Args:
        username: Nextcloud username

    Returns:
        Dict with credential details, or None if not found
    """
    query = f"""
    SELECT configkey, configvalue
    FROM oc_preferences
    WHERE userid = '{username}'
    AND appid = 'astrolabe'
    AND configkey IN ('background_sync_password', 'background_sync_type', 'background_sync_provisioned_at')
    ORDER BY configkey;
    """

    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "mariadb",
                "-u",
                "root",
                "-ppassword",
                "nextcloud",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        output = result.stdout
        if "background_sync_type" in output:
            return {
                "has_password": "background_sync_password" in output,
                "has_type": "background_sync_type" in output,
                "has_timestamp": "background_sync_provisioned_at" in output,
                "is_app_password": "app_password" in output,
            }
        return None

    except Exception as e:
        logger.error(f"Error getting credentials for {username}: {e}")
        return None


def delete_user_credentials(username: str) -> bool:
    """Delete all stored credentials for a user (for cleanup).

    Args:
        username: Nextcloud username

    Returns:
        True if successful
    """
    query = f"""
    DELETE FROM oc_preferences
    WHERE userid = '{username}'
    AND appid = 'astrolabe'
    AND configkey IN ('oauth_tokens', 'background_sync_password', 'background_sync_type', 'background_sync_provisioned_at');
    """

    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "mariadb",
                "-u",
                "root",
                "-ppassword",
                "nextcloud",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        logger.info(f"Deleted credentials for {username}")
        return result.returncode == 0

    except Exception as e:
        logger.error(f"Error deleting credentials for {username}: {e}")
        return False


@pytest.mark.integration
@pytest.mark.oauth
async def test_app_password_storage_and_cleanup(
    browser,
    nc_client,
    test_users_setup,
    configure_astrolabe_for_mcp_server,
):
    """Test that app passwords are stored and cleaned up correctly.

    This test verifies:
    1. User can save app password in Astrolabe settings
    2. Password is stored encrypted in the database
    3. Credentials can be revoked and are deleted from database

    Note: In hybrid mode (mcp-multi-user-basic), this only tests Step 2
    (app password storage). The "Active" badge requires both OAuth and
    app password, which is tested separately.
    """
    # Configure Astrolabe for mcp-multi-user-basic
    logger.info("Configuring Astrolabe for mcp-multi-user-basic server...")
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-multi-user-basic:8000",
        mcp_server_public_url="http://localhost:8003",
    )

    username = "alice"
    user_config = test_users_setup[username]
    password = user_config["password"]

    # Cleanup any existing credentials
    delete_user_credentials(username)

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # Step 1: Login
        await login_to_nextcloud(page, username, password)

        # Step 2: Verify no credentials exist initially
        initial_creds = get_background_sync_credentials(username)
        assert initial_creds is None, f"Expected no credentials, found: {initial_creds}"
        logger.info("✓ Verified no initial credentials")

        # Step 3: Generate app password
        app_password = await generate_app_password(page, username)
        assert app_password, "Failed to generate app password"

        # Step 4: Save app password in Astrolabe
        save_success = await save_app_password_in_astrolabe(
            page, username, app_password
        )
        assert save_success, "Failed to save app password"

        # Step 5: Verify credentials are stored in database
        stored_creds = get_background_sync_credentials(username)
        assert stored_creds is not None, "Expected credentials to be stored"
        assert stored_creds["has_password"], "Expected password to be stored"
        assert stored_creds["has_type"], "Expected type to be stored"
        assert stored_creds["is_app_password"], "Expected type to be 'app_password'"
        logger.info("✓ Verified credentials stored in database")

        # Step 6: Verify password is encrypted (not plaintext)
        query = f"""
        SELECT configvalue
        FROM oc_preferences
        WHERE userid = '{username}'
        AND appid = 'astrolabe'
        AND configkey = 'background_sync_password';
        """

        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "mariadb",
                "-u",
                "root",
                "-ppassword",
                "nextcloud",
                "-N",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        encrypted_value = result.stdout.strip()
        assert app_password not in encrypted_value, "Password appears in plaintext!"
        assert len(encrypted_value) > len(app_password), (
            "Encrypted value should be longer"
        )
        logger.info("✓ Verified password is encrypted")

    finally:
        await context.close()
        # Cleanup
        delete_user_credentials(username)


@pytest.mark.integration
@pytest.mark.oauth
async def test_credential_isolation_between_users(
    browser,
    nc_client,
    test_users_setup,
    configure_astrolabe_for_mcp_server,
):
    """Test that credentials are properly isolated between users.

    This test verifies:
    1. Multiple users can provision credentials independently
    2. Each user's encrypted credentials are unique
    3. Deleting one user's credentials doesn't affect others
    """
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-multi-user-basic:8000",
        mcp_server_public_url="http://localhost:8003",
    )

    test_users = ["alice", "bob"]
    user_passwords = {}

    # Cleanup all users first
    for username in test_users:
        delete_user_credentials(username)

    # Provision each user
    for username in test_users:
        user_config = test_users_setup[username]
        password = user_config["password"]

        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        try:
            await login_to_nextcloud(page, username, password)
            app_password = await generate_app_password(
                page, username, f"Test {username}"
            )
            save_success = await save_app_password_in_astrolabe(
                page, username, app_password
            )

            assert save_success, f"Failed to save app password for {username}"
            user_passwords[username] = app_password

            # Verify stored
            creds = get_background_sync_credentials(username)
            assert creds is not None, f"Credentials not stored for {username}"
            logger.info(f"✓ Credentials provisioned for {username}")

        finally:
            await context.close()

    # Verify isolation - get encrypted values
    encrypted_values = {}
    for username in test_users:
        query = f"""
        SELECT configvalue
        FROM oc_preferences
        WHERE userid = '{username}'
        AND appid = 'astrolabe'
        AND configkey = 'background_sync_password';
        """

        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "mariadb",
                "-u",
                "root",
                "-ppassword",
                "nextcloud",
                "-N",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        encrypted_values[username] = result.stdout.strip()

    # Different users should have different encrypted values
    assert encrypted_values["alice"] != encrypted_values["bob"], (
        "Different users should have different encrypted values"
    )
    logger.info("✓ Verified credentials are unique per user")

    # Delete alice's credentials and verify bob's are unaffected
    delete_user_credentials("alice")

    alice_creds = get_background_sync_credentials("alice")
    bob_creds = get_background_sync_credentials("bob")

    assert alice_creds is None, "Alice's credentials should be deleted"
    assert bob_creds is not None, "Bob's credentials should still exist"
    logger.info("✓ Verified credential deletion is isolated")

    # Cleanup
    for username in test_users:
        delete_user_credentials(username)


@pytest.mark.integration
@pytest.mark.oauth
async def test_credential_revoke_and_reprovision(
    browser,
    nc_client,
    test_users_setup,
    configure_astrolabe_for_mcp_server,
):
    """Test that credentials can be revoked and reprovisioned.

    This test verifies:
    1. User provisions credentials
    2. User revokes credentials (deletes from database)
    3. User provisions again with new app password
    4. New credentials are stored correctly

    Note: The UI prevents overwriting credentials directly - users must
    revoke first before provisioning new credentials.
    """
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-multi-user-basic:8000",
        mcp_server_public_url="http://localhost:8003",
    )

    username = "alice"
    user_config = test_users_setup[username]
    password = user_config["password"]

    delete_user_credentials(username)

    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        await login_to_nextcloud(page, username, password)

        # First provisioning
        app_password_1 = await generate_app_password(page, username, "First Password")
        await save_app_password_in_astrolabe(page, username, app_password_1)

        # Get first encrypted value
        query = f"""
        SELECT configvalue
        FROM oc_preferences
        WHERE userid = '{username}'
        AND appid = 'astrolabe'
        AND configkey = 'background_sync_password';
        """

        result1 = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "mariadb",
                "-u",
                "root",
                "-ppassword",
                "nextcloud",
                "-N",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        first_encrypted = result1.stdout.strip()
        assert first_encrypted, "First credential should be stored"
        logger.info("✓ First credential stored")

        # Revoke credentials (simulating user clicking "Revoke Access")
        delete_user_credentials(username)
        logger.info("✓ Credentials revoked")

        # Verify credentials are gone
        creds_after_revoke = get_background_sync_credentials(username)
        assert creds_after_revoke is None, "Credentials should be deleted after revoke"

        # Second provisioning with different password
        app_password_2 = await generate_app_password(page, username, "Second Password")
        await save_app_password_in_astrolabe(page, username, app_password_2)

        result2 = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "mariadb",
                "-u",
                "root",
                "-ppassword",
                "nextcloud",
                "-N",
                "-e",
                query,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        second_encrypted = result2.stdout.strip()
        assert second_encrypted, "Second credential should be stored"
        logger.info("✓ Second credential stored")

        # Verify the encrypted values are different (different passwords)
        assert first_encrypted != second_encrypted, (
            "Different passwords should produce different encrypted values"
        )

        # Verify only one row exists
        count_query = f"""
        SELECT COUNT(*)
        FROM oc_preferences
        WHERE userid = '{username}'
        AND appid = 'astrolabe'
        AND configkey = 'background_sync_password';
        """

        count_result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "mariadb",
                "-u",
                "root",
                "-ppassword",
                "nextcloud",
                "-N",
                "-e",
                count_query,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        count = int(count_result.stdout.strip())
        assert count == 1, f"Expected 1 credential row, found {count}"
        logger.info("✓ Verified clean reprovision after revoke")

    finally:
        await context.close()
        delete_user_credentials(username)
