"""Integration test for multi-user Astrolabe background sync enablement.

This test verifies that multiple users can independently:
1. Log in to Nextcloud
2. Generate an app password in Security settings
3. Enter the app password in Astrolabe personal settings
4. Enable background sync for the mcp-multi-user-basic service
5. Verify app password is stored in the database

Tests the complete app password provisioning flow:
user login → Security settings → app password generation → Astrolabe settings →
app password entry → background sync activation → database verification.
"""

import logging
import re
import subprocess

import anyio
import pytest
from playwright.async_api import Page

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


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


async def navigate_to_astrolabe_settings(page: Page):
    """Navigate to Astrolabe personal settings page.

    Args:
        page: Playwright page instance (must be authenticated)
    """
    nextcloud_url = "http://localhost:8080"
    settings_url = f"{nextcloud_url}/settings/user/astrolabe"

    logger.info(f"Navigating to Astrolabe settings: {settings_url}")
    await page.goto(settings_url, wait_until="networkidle", timeout=30000)

    # Verify we're on the settings page
    current_url = page.url
    assert "/settings/user/astrolabe" in current_url, (
        f"Failed to navigate to Astrolabe settings, current URL: {current_url}"
    )
    logger.info("✓ Successfully loaded Astrolabe settings page")


async def generate_app_password(
    page: Page, username: str, app_name: str = "Astrolabe Background Sync"
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

    # Fill the app password input field (selector confirmed via Playwright MCP)
    app_password_input = page.locator('input[placeholder="App name"]')
    await app_password_input.fill(app_name)
    logger.info(f"Entered app name: {app_name}")

    # Wait for Vue.js to react and enable the button (needs 1 second, not 0.5)
    await anyio.sleep(1.0)
    logger.info("Waited for Vue.js to process input and enable button")

    # Click the create button
    create_button = page.locator(
        'button[type="submit"]:has-text("Create new app password")'
    )
    await create_button.click()
    logger.info("Clicked create app password button")

    # Wait for app password to be generated and displayed in the dialog
    await anyio.sleep(3)  # Give it more time to generate and display

    # Find the Login input field which should have the username value
    # Then find the Password input field which is in the same form
    app_password = None
    try:
        # Wait for heading "New app password" to appear
        await page.wait_for_selector('text="New app password"', timeout=10000)
        logger.info("App password dialog appeared with heading")

        # Get all visible input elements
        all_inputs = await page.locator('input[type="text"]').all()
        logger.info(f"Found {len(all_inputs)} text input elements")

        # Check each input to find the one with the app password
        for idx, input_elem in enumerate(all_inputs):
            try:
                value = await input_elem.input_value()
                if value and "-" in value and len(value) > 20:
                    app_password = value.strip()
                    logger.info(
                        f"Found app password in input {idx}: '{app_password}' (length: {len(app_password)})"
                    )
                    break
            except Exception as e:
                logger.debug(f"Could not get value from input {idx}: {e}")
                continue

    except Exception as e:
        logger.error(f"Failed to find app password dialog or extract password: {e}")

    if not app_password:
        # Take screenshot for debugging
        screenshot_path = f"/tmp/app_password_generation_{username}.png"
        await page.screenshot(path=screenshot_path)
        raise ValueError(
            f"Could not find generated app password. Screenshot: {screenshot_path}"
        )

    # Validate password format before returning

    if not re.match(
        r"^[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}-[a-zA-Z0-9]{5}$",
        app_password,
    ):
        logger.error(
            f"Extracted password does not match expected format: '{app_password}'"
        )
        logger.error(f"Password repr: {repr(app_password)}")
        screenshot_path = f"/tmp/app_password_invalid_format_{username}.png"
        await page.screenshot(path=screenshot_path)
        raise ValueError(
            f"App password format validation failed. Screenshot: {screenshot_path}"
        )

    logger.info(
        f"✓ Generated app password for {username}: {app_password[:10]}... (validated)"
    )

    # Close the dialog by clicking the Close button
    close_button = page.get_by_role("button", name="Close")
    await close_button.click()
    logger.info("Closed app password dialog")
    await anyio.sleep(0.5)

    return app_password


async def enable_background_sync_via_app_password(
    page: Page, username: str, app_password: str
):
    """Enable background sync by entering app password in Astrolabe settings.

    Args:
        page: Playwright page instance
        username: Username (for logging)
        app_password: App password to enter

    Returns:
        True if background sync was enabled successfully
    """
    logger.info(f"Enabling background sync via app password for {username}...")

    nextcloud_url = "http://localhost:8080"

    # Set up network request and console listeners BEFORE navigation
    network_requests = []
    network_responses = []
    console_messages = []

    def log_request(req):
        network_requests.append(f"{req.method} {req.url}")

    def log_response(resp):
        response_info = f"{resp.status} {resp.url}"
        network_responses.append(response_info)
        logger.info(f"Response: {response_info}")

    def log_console(msg):
        console_messages.append(f"[{msg.type}] {msg.text}")

    page.on("request", log_request)
    page.on("response", log_response)
    page.on("console", log_console)

    # Navigate to Astrolabe settings
    await page.goto(
        f"{nextcloud_url}/settings/user/astrolabe", wait_until="networkidle"
    )

    # Wait for page to load
    await anyio.sleep(1)

    # Check if already active (look for "Active" text in the Background Sync Access section)
    try:
        # The "Active" badge appears as a <span> with text "Active"
        active_text = page.get_by_text("Active", exact=True)
        if await active_text.is_visible(timeout=2000):
            logger.info(f"✓ Background sync already active for {username}")
            return True
    except Exception:
        pass

    # Find the app password input field using the placeholder text
    # Based on manual testing: textbox with placeholder "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"
    app_password_input = page.get_by_placeholder("xxxxx-xxxxx-xxxxx-xxxxx-xxxxx")

    try:
        await app_password_input.wait_for(timeout=5000, state="visible")
        logger.info("Found app password input field")
    except Exception:
        # Take screenshot for debugging
        screenshot_path = f"/tmp/astrolabe_no_password_field_{username}.png"
        await page.screenshot(path=screenshot_path)
        raise ValueError(
            f"Could not find app password input field for {username}. Screenshot: {screenshot_path}"
        )

    # Enter the app password
    await app_password_input.fill(app_password)
    logger.info(f"Entered app password for {username}")

    # Wait a moment for any validation to complete
    await anyio.sleep(0.5)

    # Take screenshot before clicking Save to check for warnings
    screenshot_path = f"/tmp/before_save_{username}.png"
    await page.screenshot(path=screenshot_path)
    logger.info(f"Screenshot taken before Save: {screenshot_path}")

    # Find and click the Save button
    save_button = page.get_by_role("button", name="Save")

    # Check if Save button is enabled
    is_disabled = await save_button.is_disabled()
    logger.info(f"Save button disabled state: {is_disabled}")

    await save_button.click()
    logger.info("Clicked Save button")

    # Give the request time to complete before checking logs
    await anyio.sleep(0.5)

    # Log network requests after clicking Save
    logger.info(f"Network requests after Save for {username}:")
    for req in network_requests[-10:]:  # Last 10 requests
        logger.info(f"  {req}")

    # Log network responses after clicking Save
    logger.info(f"Network responses after Save for {username}:")
    for resp in network_responses[-10:]:  # Last 10 responses
        logger.info(f"  {resp}")

    # Check specifically for the credentials POST response
    credentials_responses = [
        r for r in network_responses if "background-sync/credentials" in r
    ]
    if credentials_responses:
        logger.info(f"Credentials endpoint response: {credentials_responses[-1]}")
        if "200" not in credentials_responses[-1]:
            logger.error(
                f"Credentials POST did not return 200 OK: {credentials_responses[-1]}"
            )
    else:
        logger.warning("No response found for credentials endpoint!")

    # Wait for the page to reload after successful save
    # The JavaScript in personalSettings.js does: setTimeout(() => window.location.reload(), 1000)
    await page.wait_for_load_state("networkidle", timeout=15000)
    await anyio.sleep(2)

    # Log any console messages
    if console_messages:
        logger.info(f"Console messages for {username}:")
        for msg in console_messages:
            logger.info(f"  {msg}")

    # Check for error notifications (toast messages)
    try:
        error_toast = page.locator(".toastify.toast-error, .toast-error")
        if await error_toast.count() > 0:
            error_text = await error_toast.first.text_content()
            logger.error(f"Error notification for {username}: {error_text}")
    except Exception:
        pass

    # Verify "Active" text appears after reload
    try:
        active_text = page.get_by_text("Active", exact=True)
        await active_text.wait_for(timeout=5000, state="visible")
        logger.info(f"✓ Background sync enabled for {username} - Active badge visible")
        return True
    except Exception:
        # Take screenshot for debugging
        screenshot_path = f"/tmp/astrolabe_after_password_{username}.png"
        await page.screenshot(path=screenshot_path)
        logger.error(
            f"Active badge did not appear for {username}. Screenshot: {screenshot_path}"
        )
        raise


async def verify_app_password_created(username: str) -> bool:
    """Verify that background sync app password was stored for the user.

    This checks the Nextcloud database for background sync credentials stored
    by Astrolabe in the oc_preferences table.

    Args:
        username: Nextcloud username

    Returns:
        True if background sync app password exists
    """
    logger.info(f"Verifying background sync app password for {username}...")

    # Query the database to check for background sync credentials
    # Astrolabe stores app passwords in oc_preferences, not oc_authtoken

    query = f"""
    SELECT userid, configkey, configvalue
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
        logger.debug(f"Background sync credentials query result:\n{output}")

        # Check if background sync credentials exist
        # We should see 3 rows: background_sync_password, background_sync_type, background_sync_provisioned_at
        lines = output.strip().split("\n")

        if len(lines) >= 3:  # Header + at least 2 data rows (password + type)
            # Verify background_sync_type is "app_password"
            if "app_password" in output:
                logger.info(f"✓ Background sync app password stored for {username}")
                return True
            else:
                logger.warning(
                    f"Background sync credentials found but type is not app_password for {username}"
                )
                return False
        else:
            logger.warning(f"No background sync credentials found for {username}")
            return False

    except Exception as e:
        logger.error(f"Error checking background sync credentials for {username}: {e}")
        return False


@pytest.mark.integration
@pytest.mark.oauth
async def test_multi_user_astrolabe_background_sync_enablement(
    browser,
    nc_client,
    test_users_setup,
    configure_astrolabe_for_mcp_server,
):
    """Test that multiple users can independently enable background sync via app passwords.

    This test verifies the complete app password provisioning flow:
    1. Users log in to Nextcloud
    2. Users generate app passwords in Security settings
    3. Users navigate to Astrolabe personal settings
    4. Users enter their app passwords in the Astrolabe form
    5. Background sync becomes active with "Active" badge
    6. App passwords are stored in the database (oc_authtoken table)
    7. The process works correctly for multiple users

    Requirements:
    - Astrolabe app installed in Nextcloud and configured for mcp-multi-user-basic
    - MCP server running in multi-user BasicAuth mode (mcp-multi-user-basic service)
    - Test users (alice, bob) created with valid credentials

    This tests ADR-002 Tier 2 authentication: User-specific app passwords for background operations
    in multi-user BasicAuth deployments.
    """
    # Configure Astrolabe to point to the mcp-multi-user-basic server
    logger.info("Configuring Astrolabe for mcp-multi-user-basic server...")
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-multi-user-basic:8000",
        mcp_server_public_url="http://localhost:8003",
    )

    # Test users to check
    test_users = ["alice", "bob"]

    # Verify test users were created by the fixture
    logger.info("Verifying test users exist in Nextcloud...")
    for username in test_users:
        try:
            # Use nc_client to check if user exists
            user_details = await nc_client.users.get_user_details(username)
            logger.info(
                f"✓ Confirmed {username} exists (display name: {user_details.displayname})"
            )
        except Exception as e:
            raise AssertionError(
                f"Test user {username} does not exist! "
                f"test_users_setup fixture may have failed. Error: {e}"
            )

    results = {}

    for username in test_users:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Testing background sync enablement for: {username}")
        logger.info(f"{'=' * 60}")

        user_config = test_users_setup[username]
        password = user_config["password"]

        # Create new browser context for this user
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        try:
            # Step 1: Login to Nextcloud
            await login_to_nextcloud(page, username, password)

            # Step 2: Generate app password in Security settings
            app_password = await generate_app_password(page, username)

            # Step 3: Enable background sync by entering app password in Astrolabe
            sync_enabled = await enable_background_sync_via_app_password(
                page, username, app_password
            )

            # Step 4: Verify app password was stored in database
            app_password_stored = await verify_app_password_created(username)

            # Give it time to complete
            await anyio.sleep(1)

            results[username] = {
                "settings_accessed": True,
                "app_password_generated": bool(app_password),
                "sync_enabled": sync_enabled,
                "app_password_stored": app_password_stored,
                "background_sync_active": sync_enabled and app_password_stored,
            }

            logger.info(f"\n{username} results:")
            logger.info("  Settings accessed: ✓")
            logger.info(f"  App password generated: {'✓' if app_password else '✗'}")
            logger.info(f"  Sync enabled: {'✓' if sync_enabled else '✗'}")
            logger.info(f"  App password stored: {'✓' if app_password_stored else '✗'}")
            logger.info(
                f"  Background sync active: {'✓' if (sync_enabled and app_password_stored) else '✗'}"
            )

        except Exception as e:
            logger.error(f"Error during {username} test: {e}")
            results[username] = {
                "settings_accessed": False,
                "app_password_generated": False,
                "sync_enabled": False,
                "app_password_stored": False,
                "background_sync_active": False,
                "error": str(e),
            }

        finally:
            await context.close()

    # Verify all users succeeded
    logger.info(f"\n{'=' * 60}")
    logger.info("Test Summary")
    logger.info(f"{'=' * 60}")

    for username, result in results.items():
        logger.info(f"\n{username}:")
        for key, value in result.items():
            if key != "error":
                status = "✓" if value else "✗"
                logger.info(f"  {key}: {status}")
            elif value:
                logger.info(f"  error: {value}")

    # Assert all users successfully enabled background sync
    for username in test_users:
        result = results[username]
        assert result["settings_accessed"], (
            f"{username} could not access Astrolabe settings"
        )
        assert result["app_password_generated"], (
            f"{username} app password was not generated"
        )
        assert result["sync_enabled"], (
            f"{username} background sync enablement did not complete successfully"
        )
        assert result["app_password_stored"], (
            f"{username} app password was not stored in database"
        )
        assert result["background_sync_active"], (
            f"{username} background sync is not active"
        )

    logger.info(
        f"\n✓ All {len(test_users)} users successfully enabled background sync via app passwords!"
    )
