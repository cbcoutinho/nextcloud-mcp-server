"""Integration test for multi-user Astrolabe background sync enablement.

Cross-system interface test: Tests the MCP server's integration with the
Astrolabe Nextcloud app, which is installed from the Nextcloud app store via
app-hooks/post-installation/20-install-astrolabe-app.sh. Astrolabe source
lives in a separate repository (https://github.com/cbcoutinho/astrolabe).

This test verifies that multiple users can independently:
1. Log in to Nextcloud
2. Click the one-click "Enable background indexing" opt-in in Astrolabe settings
3. Have a dedicated app password minted from their session and handed to the
   MCP server (core/getapppassword — no Security-settings step, no copy-paste)
4. Verify the app password is stored in the database

Tests the one-click background-indexing provisioning flow:
user login → Astrolabe settings → Enable background indexing → session app
password minted + forwarded to MCP → background sync active → DB verification.
"""

import logging
import subprocess
import tempfile

import anyio
import pytest
from playwright.async_api import Page

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.multi_user_basic]


async def login_to_nextcloud(page: Page, username: str, password: str):
    """Helper function to login to Nextcloud via Playwright.

    Args:
        page: Playwright page instance
        username: Nextcloud username
        password: Nextcloud password
    """
    nextcloud_url = "http://localhost:8080"

    logger.info("Logging in to Nextcloud as %s...", username)
    await page.goto(f"{nextcloud_url}/login", wait_until="networkidle")

    # Fill in login form
    await page.wait_for_selector('input[name="user"]', timeout=10000)
    await page.fill('input[name="user"]', username)
    await page.fill('input[name="password"]', password)

    # Submit form - use force=True to bypass stability check (CSS transitions)
    submit_button = page.locator('button[type="submit"]')
    try:
        await submit_button.click(force=True, timeout=10000)
    except Exception:
        # Fallback: JavaScript click
        logger.info("Using JavaScript click for login button...")
        await page.evaluate(
            """
            const btn = document.querySelector('button[type="submit"]');
            if (btn) btn.click();
            """
        )
    await page.wait_for_load_state("networkidle", timeout=30000)

    # Verify logged in (should redirect away from login page)
    current_url = page.url
    assert "/login" not in current_url, (
        f"Login failed for {username}, still on login page"
    )
    logger.info("✓ Successfully logged in as %s", username)


async def navigate_to_astrolabe_settings(page: Page):
    """Navigate to Astrolabe personal settings page.

    Args:
        page: Playwright page instance (must be authenticated)
    """
    nextcloud_url = "http://localhost:8080"
    settings_url = f"{nextcloud_url}/settings/user/astrolabe"

    logger.info("Navigating to Astrolabe settings: %s", settings_url)
    await page.goto(settings_url, wait_until="networkidle", timeout=30000)

    # Verify we're on the settings page
    current_url = page.url
    assert "/settings/user/astrolabe" in current_url, (
        f"Failed to navigate to Astrolabe settings, current URL: {current_url}"
    )
    logger.info("✓ Successfully loaded Astrolabe settings page")


async def enable_background_sync(page: Page, username: str) -> bool:
    """Provision background indexing via the one-click opt-in button.

    The refactored settings page mints a dedicated app password from the
    current Nextcloud session (core/getapppassword) and hands it to the MCP
    server — there is no app-password generation in Security settings and no
    copy-paste. Idempotent: if already enabled (the revoke form is shown
    instead of the enable button), returns True without acting.

    Args:
        page: Playwright page instance (must be logged in)
        username: Username (for logging)

    Returns:
        True once background indexing is enabled.
    """
    logger.info("Enabling background indexing for %s...", username)
    await page.goto(
        "http://localhost:8080/settings/user/astrolabe", wait_until="networkidle"
    )
    await anyio.sleep(1)

    if await page.locator("#mcp-revoke-background-button").count() > 0:
        logger.info("✓ Background indexing already enabled for %s", username)
        return True

    enable_button = page.locator("#mcp-enable-background-button")
    await enable_button.wait_for(timeout=5000, state="visible")
    await enable_button.click()
    logger.info("Clicked 'Enable background indexing' for %s", username)

    # On success the page JS reloads to the enabled state (revoke form shown).
    try:
        await page.locator("#mcp-revoke-background-button").wait_for(
            timeout=15000, state="visible"
        )
        logger.info("✓ Background indexing enabled for %s", username)
        return True
    except Exception:
        screenshot_path = (
            f"{tempfile.gettempdir()}/astrolabe_enable_failed_{username}.png"
        )
        await page.screenshot(path=screenshot_path)
        raise ValueError(
            f"Background indexing did not enable for {username}. "
            f"Screenshot: {screenshot_path}"
        )


async def complete_astrolabe_authorization(
    page: Page, username: str, password: str
) -> dict:
    """Provision background indexing for a user (one-click app-password opt-in).

    The auth refactor dropped the per-user OAuth step entirely — search now
    uses a session-minted JWT, so the only remaining "authorization" is the
    one-click background-indexing opt-in (a dedicated app password minted from
    the session and handed to the MCP server).

    Args:
        page: Playwright page instance (must be logged in)
        username: Nextcloud username
        password: Nextcloud password (for reference, not used directly)

    Returns:
        Dict with {"step1": True (no-op, kept for caller compat),
        "step2": bool, "app_password": None}
    """
    logger.info("Provisioning Astrolabe background indexing for %s...", username)

    # step1 is retained as always-True for backward compat with callers — there
    # is no longer an OAuth authorize step to perform.
    result = {"step1": True, "step2": False, "app_password": None}
    result["step2"] = await enable_background_sync(page, username)
    return result


async def verify_app_password_created(username: str) -> bool:
    """Verify that background sync app password was stored for the user.

    This checks the Nextcloud database for background sync credentials stored
    by Astrolabe in the oc_preferences table.

    Args:
        username: Nextcloud username

    Returns:
        True if background sync app password exists
    """
    logger.info("Verifying background sync app password for %s...", username)

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
        logger.debug("Background sync credentials query result:\\n%s", output)

        # Check if background sync credentials exist
        # We should see 3 rows: background_sync_password, background_sync_type, background_sync_provisioned_at
        lines = output.strip().split("\n")

        if len(lines) >= 3:  # Header + at least 2 data rows (password + type)
            # Verify background_sync_type is "app_password"
            if "app_password" in output:
                logger.info("✓ Background sync app password stored for %s", username)
                return True
            else:
                logger.warning(
                    "Background sync credentials found but type is not app_password for %s",
                    username,
                )
                return False
        else:
            logger.warning("No background sync credentials found for %s", username)
            return False

    except Exception as e:
        logger.error(
            "Error checking background sync credentials for %s: %s", username, e
        )
        return False


def clear_stale_test_state(clear_preferences: bool = False) -> None:
    """Clear stale app passwords, bruteforce entries, and optionally Astrolabe preferences."""
    commands: list[tuple[list[str], str]] = [
        (
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "mcp-multi-user-basic",
                "sqlite3",
                "/app/data/tokens.db",
                "DELETE FROM app_passwords;",
            ],
            "app passwords",
        ),
        (
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
                "DELETE FROM oc_bruteforce_attempts;",
            ],
            "bruteforce entries",
        ),
    ]
    if clear_preferences:
        commands.append(
            (
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
                    "DELETE FROM oc_preferences WHERE appid = 'astrolabe';",
                ],
                "Astrolabe preferences",
            ),
        )
    for cmd, label in commands:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            logger.warning(
                "Failed to clear %s (rc=%s): %s",
                label,
                result.returncode,
                result.stderr,
            )
        else:
            logger.debug("Cleared %s", label)


@pytest.mark.integration
@pytest.mark.multi_user_basic
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
    # Clear stale state from previous test runs
    logger.info("Clearing stale app passwords and bruteforce entries...")
    clear_stale_test_state()

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
                "✓ Confirmed %s exists (display name: %s)",
                username,
                user_details.displayname,
            )
        except Exception as e:
            raise AssertionError(
                f"Test user {username} does not exist! "
                f"test_users_setup fixture may have failed. Error: {e}"
            )

    results = {}

    for username in test_users:
        logger.info("\\n%s", "=" * 60)
        logger.info("Testing background sync enablement for: %s", username)
        logger.info("%s", "=" * 60)

        user_config = test_users_setup[username]
        password = user_config["password"]

        # Create new browser context for this user
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        try:
            # Step 1: Login to Nextcloud
            await login_to_nextcloud(page, username, password)

            # Step 2: One-click "Enable background indexing" (mints a dedicated
            # app password from the session and hands it to the MCP server).
            sync_enabled = await enable_background_sync(page, username)

            # Step 3: Verify app password was stored in database
            app_password_stored = await verify_app_password_created(username)

            # Give it time to complete
            await anyio.sleep(1)

            results[username] = {
                "settings_accessed": True,
                "sync_enabled": sync_enabled,
                "app_password_stored": app_password_stored,
                "background_sync_active": sync_enabled and app_password_stored,
            }

            logger.info("\\n%s results:", username)
            logger.info("  Settings accessed: ✓")
            logger.info("  Sync enabled: %s", "✓" if sync_enabled else "✗")
            logger.info(
                "  App password stored: %s", "✓" if app_password_stored else "✗"
            )
            logger.info(
                "  Background sync active: %s",
                "✓" if (sync_enabled and app_password_stored) else "✗",
            )

        except Exception as e:
            logger.error("Error during %s test: %s", username, e)
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
    logger.info("\\n%s", "=" * 60)
    logger.info("Test Summary")
    logger.info("%s", "=" * 60)

    for username, result in results.items():
        logger.info("\\n%s:", username)
        for key, value in result.items():
            if key != "error":
                status = "✓" if value else "✗"
                logger.info("  %s: %s", key, status)
            elif value:
                logger.info("  error: %s", value)

    # Assert all users successfully enabled background sync
    for username in test_users:
        result = results[username]
        assert result["settings_accessed"], (
            f"{username} could not access Astrolabe settings"
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
        "\\n✓ All %s users successfully enabled background sync via app passwords!",
        len(test_users),
    )


async def revoke_background_sync_access(page: Page, username: str) -> bool:
    """Revoke background sync access by clicking the "Disable background indexing" button.

    Args:
        page: Playwright page instance (must be authenticated)
        username: Username (for logging)

    Returns:
        True if revocation was successful
    """
    logger.info("Revoking background sync access for %s...", username)

    nextcloud_url = "http://localhost:8080"

    # Set up network request and console listeners
    network_requests = []
    network_responses = []
    console_messages = []

    def log_request(req):
        network_requests.append(f"{req.method} {req.url}")

    def log_response(resp):
        response_info = f"{resp.status} {resp.url}"
        network_responses.append(response_info)
        logger.info("Response: %s", response_info)

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

    # The revoke form (#mcp-revoke-background-form) is only rendered while
    # background indexing is enabled.
    revoke_button = page.locator("#mcp-revoke-background-button")
    try:
        if await revoke_button.count() == 0:
            logger.warning(
                "Background indexing not enabled for %s, nothing to revoke", username
            )
            return False
    except Exception:
        logger.warning("Could not find revoke button for %s", username)
        return False

    try:
        await revoke_button.wait_for(timeout=5000, state="visible")
        logger.info("Found 'Disable background indexing' button")
    except Exception:
        screenshot_path = (
            f"{tempfile.gettempdir()}/astrolabe_no_revoke_button_{username}.png"
        )
        await page.screenshot(path=screenshot_path)
        raise ValueError(
            f"Could not find revoke button for {username}. Screenshot: {screenshot_path}"
        )

    # Set up dialog handler for confirmation dialog
    page.once("dialog", lambda dialog: dialog.accept())

    # Click the "Disable background indexing" button
    await revoke_button.click()
    logger.info("Clicked the revoke button")

    # Wait for the request to complete and page to reload
    await page.wait_for_load_state("networkidle", timeout=15000)
    await anyio.sleep(2)

    # Log network requests after clicking
    logger.info("Network requests after Revoke for %s:", username)
    for req in network_requests[-10:]:
        logger.info("  %s", req)

    # Log network responses
    logger.info("Network responses after Revoke for %s:", username)
    for resp in network_responses[-10:]:
        logger.info("  %s", resp)

    # Check specifically for the revoke POST response
    revoke_responses = [r for r in network_responses if "credentials/revoke" in r]
    if revoke_responses:
        logger.info("Revoke endpoint response: %s", revoke_responses[-1])
        if "200" not in revoke_responses[-1]:
            logger.error("Revoke POST did not return 200 OK: %s", revoke_responses[-1])
            return False
    else:
        logger.warning("No response found for credentials/revoke endpoint!")
        # Take screenshot for debugging
        screenshot_path = (
            f"{tempfile.gettempdir()}/astrolabe_revoke_no_response_{username}.png"
        )
        await page.screenshot(path=screenshot_path)
        return False

    # Log any console messages
    if console_messages:
        logger.info("Console messages for %s:", username)
        for msg in console_messages:
            logger.info("  %s", msg)

    # Check for error notifications (toast messages)
    try:
        error_toast = page.locator(".toastify.toast-error, .toast-error")
        if await error_toast.count() > 0:
            error_text = await error_toast.first.text_content()
            logger.error("Error notification for %s: %s", username, error_text)
            return False
    except Exception:
        pass

    # After revoke + reload the settings page returns to the un-provisioned
    # state: the revoke button is gone and the app-password input is shown again.
    try:
        if await page.locator("#mcp-revoke-background-button").is_visible(timeout=2000):
            logger.error("Revoke button still visible for %s after revoke!", username)
            screenshot_path = (
                f"{tempfile.gettempdir()}/astrolabe_revoke_still_enabled_{username}.png"
            )
            await page.screenshot(path=screenshot_path)
            return False
    except Exception:
        pass

    logger.info("✓ Background sync access revoked for %s", username)
    return True


async def verify_app_password_deleted(username: str) -> bool:
    """Verify that background sync app password was deleted for the user.

    Args:
        username: Nextcloud username

    Returns:
        True if background sync credentials no longer exist
    """
    logger.info("Verifying background sync credentials deleted for %s...", username)

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
        logger.debug("Background sync credentials query result:\\n%s", output)

        # After deletion, we should NOT see background_sync_password
        if "background_sync_password" not in output:
            logger.info("✓ Background sync credentials deleted for %s", username)
            return True
        else:
            logger.warning("Background sync credentials still exist for %s", username)
            return False

    except Exception as e:
        logger.error(
            "Error checking background sync credentials for %s: %s", username, e
        )
        return False


@pytest.mark.integration
@pytest.mark.multi_user_basic
async def test_revoke_background_sync_access(
    browser,
    nc_client,
    test_users_setup,
    configure_astrolabe_for_mcp_server,
):
    """Test that users can revoke background sync access via the "Disable background indexing" button.

    This test verifies:
    1. User enables background sync via app password
    2. User clicks "Disable background indexing" button
    3. Confirmation dialog is handled
    4. POST request is sent to /api/v1/background-sync/credentials/revoke
    5. "Active" badge disappears from settings page
    6. Background sync credentials are deleted from database

    This tests the fix for the issue where POST requests to the revoke endpoint
    were returning errors due to HTTP method mismatch (was DELETE, now POST).
    """
    # Clear stale state from previous test runs
    logger.info(
        "Clearing stale app passwords, bruteforce entries, and Astrolabe preferences..."
    )
    clear_stale_test_state(clear_preferences=True)

    # Configure Astrolabe to point to the mcp-multi-user-basic server
    logger.info("Configuring Astrolabe for mcp-multi-user-basic server...")
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-multi-user-basic:8000",
        mcp_server_public_url="http://localhost:8003",
    )

    # Test with a single user for this specific test
    username = "alice"
    user_config = test_users_setup[username]
    password = user_config["password"]

    # Create new browser context
    context = await browser.new_context(ignore_https_errors=True)
    page = await context.new_page()

    try:
        # Step 1: Login to Nextcloud
        await login_to_nextcloud(page, username, password)

        # Provision background indexing (app-password opt-in; no OAuth step).
        auth_result = await complete_astrolabe_authorization(page, username, password)
        assert auth_result["step2"], f"App password provisioning failed for {username}"

        # Step 3: Verify background sync is enabled
        assert await verify_app_password_created(username), (
            f"Background sync not enabled for {username}"
        )

        # Step 4: Revoke background sync access
        revoke_success = await revoke_background_sync_access(page, username)
        assert revoke_success, f"Failed to revoke background sync access for {username}"

        # Step 5: Verify credentials are deleted from database
        credentials_deleted = await verify_app_password_deleted(username)
        assert credentials_deleted, (
            f"Background sync credentials not deleted for {username}"
        )

        logger.info(
            "\\n✓ Successfully revoked background sync access for %s!", username
        )

    finally:
        await context.close()
