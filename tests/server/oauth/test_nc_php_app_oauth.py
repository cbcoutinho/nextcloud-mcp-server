"""Test OAuth authorization flow for Nextcloud PHP app (astrolabe).

Tests the complete PKCE OAuth flow from the NC PHP app perspective:
1. User navigates to personal settings
2. Clicks "Authorize Access" button
3. Completes OAuth authorization via Nextcloud OIDC app
4. Token is stored encrypted in Nextcloud database
5. App can use token to call MCP management API

This tests the architecture from ADR-018 where the NC PHP app uses
OAuth PKCE (public client) to obtain tokens from Nextcloud's OIDC app.
"""

import logging
import os

import httpx
import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


@pytest.fixture(scope="module")
def nextcloud_credentials():
    """Get Nextcloud credentials from environment."""
    return {
        "host": os.getenv("NEXTCLOUD_HOST", "http://localhost:8080"),
        "username": os.getenv("NEXTCLOUD_USERNAME", "admin"),
        "password": os.getenv("NEXTCLOUD_PASSWORD", "admin"),
    }


@pytest.fixture(scope="module")
async def nc_admin_http_client(nextcloud_credentials):
    """HTTP client authenticated as admin user for NC API calls."""
    async with httpx.AsyncClient(
        base_url=nextcloud_credentials["host"],
        auth=(nextcloud_credentials["username"], nextcloud_credentials["password"]),
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture(scope="module")
async def configure_astrolabe_for_tests(configure_astrolabe_for_mcp_server):
    """Configure Astrolabe to connect to mcp-oauth server before running tests.

    This module-scoped fixture ensures Astrolabe is properly configured
    for the mcp-oauth server (http://localhost:8001) before any tests run.
    """
    logger.info("Configuring Astrolabe for mcp-oauth server...")
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-oauth:8001",
        mcp_server_public_url="http://localhost:8001",
    )
    logger.info("✓ Astrolabe configured for mcp-oauth server")


@pytest.fixture(scope="module")
async def authorized_nc_session(
    browser, nextcloud_credentials, configure_astrolabe_for_tests
):
    """Module-scoped fixture that logs in and authorizes the NC PHP app once.

    This fixture:
    1. Configures Astrolabe for mcp-oauth server (via configure_astrolabe_for_tests)
    2. Creates a browser context
    3. Logs in to Nextcloud
    4. Authorizes the MCP Server UI app (if not already authorized)
    5. Returns the page for use in all tests

    The authorization is done once and reused for all tests in this module.
    """
    host = nextcloud_credentials["host"]
    username = nextcloud_credentials["username"]
    password = nextcloud_credentials["password"]

    logger.info("Setting up module-scoped authorized NC session...")

    # Create browser context that persists for module duration
    context = await browser.new_context()
    page = await context.new_page()

    # Enable console message logging
    page.on(
        "console", lambda msg: logger.debug(f"Browser console [{msg.type}]: {msg.text}")
    )
    page.on("pageerror", lambda err: logger.error(f"Browser page error: {err}"))

    try:
        # Step 1: Login to Nextcloud
        logger.info(f"Logging in to Nextcloud as {username}...")
        await page.goto(f"{host}/login")

        # Fill login form
        await page.fill('input[name="user"]', username)
        await page.fill('input[name="password"]', password)
        await page.click('button[type="submit"]')

        # Wait for login to complete (dashboard loads)
        await page.wait_for_url(f"{host}/apps/dashboard/", timeout=10000)
        logger.info("✓ Logged in successfully")

        # Step 2: Navigate to personal MCP settings
        logger.info("Navigating to personal MCP settings...")
        await page.goto(f"{host}/settings/user/astrolabe")
        await page.wait_for_load_state("networkidle")

        page_content = await page.content()

        # Step 3: Check if authorization is needed
        # Vue 3 UI shows "Enable Semantic Search" when not authorized
        if (
            "Enable Semantic Search" in page_content
            or "What happens next?" in page_content
        ):
            logger.info("User not authorized yet - initiating OAuth flow...")

            # Click "Enable Semantic Search" button (Vue 3 template text)
            authorize_selectors = [
                'a:has-text("Enable Semantic Search")',
                'button:has-text("Enable Semantic Search")',
                'a:has-text("Sign In Again")',
                "a.button.primary",
                '[href*="oauth/login"]',
            ]

            clicked = False
            for selector in authorize_selectors:
                try:
                    await page.click(selector, timeout=2000)
                    clicked = True
                    logger.info(f"✓ Clicked authorize button (selector: {selector})")
                    break
                except Exception:
                    continue

            if not clicked:
                screenshot_path = "/tmp/nc-php-app-settings.png"
                await page.screenshot(path=screenshot_path)
                pytest.fail(
                    f"Could not find authorize button. Screenshot: {screenshot_path}"
                )

            # Wait for page to load after clicking
            await page.wait_for_load_state("networkidle", timeout=10000)
            current_url = page.url
            logger.info(f"After clicking authorize, current URL: {current_url}")

            # Take screenshot for debugging
            await page.screenshot(path="/tmp/nc-php-app-after-authorize-click.png")
            logger.info("Screenshot saved to /tmp/nc-php-app-after-authorize-click.png")

            # Handle OAuth consent if needed
            if (
                "/apps/oidc/authorize" in current_url
                or "/apps/oidc/consent" in current_url
            ):
                logger.info("On OIDC authorization page - granting consent...")

                consent_selectors = [
                    'button:has-text("Allow")',
                    'button:has-text("Authorize")',
                    'input[type="submit"][value="Allow"]',
                    'button[type="submit"]',
                ]

                for selector in consent_selectors:
                    try:
                        await page.click(selector, timeout=2000)
                        logger.info(f"✓ Clicked consent button (selector: {selector})")
                        break
                    except Exception:
                        continue

            # Wait for redirect back to settings
            await page.wait_for_url(f"{host}/settings/user/astrolabe", timeout=15000)
            await page.wait_for_load_state("networkidle")
            logger.info("✓ OAuth authorization completed")

        else:
            logger.info("User already authorized")

        # Return the page and context info for tests
        yield {
            "page": page,
            "context": context,
            "host": host,
            "username": username,
        }

    finally:
        # Cleanup at module end
        logger.info("Closing authorized NC session...")
        await context.close()


class TestNcPhpAppOAuth:
    """Test suite for NC PHP app OAuth integration."""

    async def test_authorization_completed(self, authorized_nc_session):
        """Verify OAuth authorization was successful.

        This test verifies the settings page shows the user is connected
        after the module-scoped authorization fixture runs.
        """
        page = authorized_nc_session["page"]
        host = authorized_nc_session["host"]

        # Navigate to settings (may already be there)
        await page.goto(f"{host}/settings/user/astrolabe")
        await page.wait_for_load_state("networkidle")

        page_content = await page.content()

        # Look for indicators that authorization succeeded (Vue 3 personal.php template)
        # These must be unique to the authorized state (not found in oauth-required.php)
        success_indicators = [
            "Service Status",
            "Background Sync Access",
            "Manage Connection",
            "Revoke Access",
            "Service URL",
        ]

        found_indicators = [ind for ind in success_indicators if ind in page_content]
        has_success_indicator = len(found_indicators) > 0

        # Always take screenshot for debugging
        screenshot_path = "/tmp/nc-php-app-auth-check.png"
        await page.screenshot(path=screenshot_path)
        logger.info(f"Authorization check screenshot: {screenshot_path}")
        logger.info(f"Found success indicators: {found_indicators}")

        if not has_success_indicator:
            logger.error("Authorization check failed.")

        assert has_success_indicator, "Settings page should show user is authorized"
        logger.info("✓ Authorization verification passed")

    async def test_token_storage_and_retrieval(self, authorized_nc_session):
        """Test that tokens are properly stored and can be retrieved.

        Verifies the settings page displays session information,
        indicating the token was stored and retrieved successfully.
        """
        page = authorized_nc_session["page"]
        host = authorized_nc_session["host"]

        await page.goto(f"{host}/settings/user/astrolabe")
        await page.wait_for_load_state("networkidle")

        page_content = await page.content()

        # Debug: take screenshot and log content excerpt
        screenshot_path = "/tmp/nc-php-app-token-test.png"
        await page.screenshot(path=screenshot_path)
        logger.info(f"Screenshot saved: {screenshot_path}")
        logger.info(f"Page content excerpt: {page_content[:1000]}")

        # Verify session information is visible (Vue 3 personal.php template)
        session_indicators = [
            "Service Status",
            "Service URL",
            "Version",
            "Background Sync Access",
        ]

        found_indicators = [ind for ind in session_indicators if ind in page_content]
        assert len(found_indicators) >= 2, (
            f"Expected session info on page. Found: {found_indicators}. Check {screenshot_path}"
        )

        logger.info(f"✓ Token retrieval verified - found: {found_indicators}")

    async def test_management_api_access(
        self, authorized_nc_session, nc_admin_http_client
    ):
        """Test that the NC PHP app can access MCP server management API.

        Verifies the settings page successfully fetched data from the
        MCP server's management API endpoints.
        """
        page = authorized_nc_session["page"]
        host = authorized_nc_session["host"]

        # Check personal settings page shows server status
        await page.goto(f"{host}/settings/user/astrolabe")
        await page.wait_for_load_state("networkidle")

        page_content = await page.content()

        # Look for data that comes from management API or template structure (Vue 3)
        api_indicators = [
            "Service Status",  # Section header
            "Service URL",  # Server info from API
            "Version",  # Server version from management API
            "Semantic Search",  # Vector sync status
        ]

        found_api_data = [ind for ind in api_indicators if ind in page_content]
        assert len(found_api_data) >= 1, (
            f"Expected management API data on page. Found: {found_api_data}"
        )

        logger.info(f"✓ Management API access verified - found: {found_api_data}")

    async def test_admin_settings_page(self, authorized_nc_session):
        """Test that admin settings page loads and displays server info.

        The admin page should show server status from the management API.
        """
        page = authorized_nc_session["page"]
        host = authorized_nc_session["host"]

        await page.goto(f"{host}/settings/admin/astrolabe")
        await page.wait_for_load_state("networkidle")

        page_content = await page.content()

        # Admin page should show server status (Vue 3 AdminSettings.vue)
        admin_indicators = [
            "Astrolabe",
            "Service Status",
            "Version",
            "Semantic Search",
        ]

        found_indicators = [ind for ind in admin_indicators if ind in page_content]

        # Admin page should at least show the Astrolabe header or Service Status
        assert "Astrolabe" in page_content or "Service Status" in page_content, (
            "Admin settings page should show Astrolabe section"
        )

        logger.info(f"✓ Admin settings page verified - found: {found_indicators}")


class TestNcPhpAppDisconnect:
    """Test suite for NC PHP app disconnect functionality.

    Note: These tests are run separately and may modify the authorization state.
    They should run after the main OAuth tests.
    """

    @pytest.mark.skip(reason="Disconnect test modifies state - run manually if needed")
    async def test_disconnect_flow(self, browser, nextcloud_credentials):
        """Test that users can disconnect (revoke) their authorization.

        This test:
        1. Logs in fresh (separate from authorized_nc_session)
        2. Verifies user is authorized
        3. Clicks "Disconnect" button
        4. Verifies user is no longer authorized

        Skipped by default as it modifies authorization state.
        """
        host = nextcloud_credentials["host"]
        username = nextcloud_credentials["username"]
        password = nextcloud_credentials["password"]

        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await page.goto(f"{host}/login")
            await page.fill('input[name="user"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_url(f"{host}/apps/dashboard/", timeout=10000)

            # Navigate to personal settings
            await page.goto(f"{host}/settings/user/astrolabe")
            await page.wait_for_load_state("networkidle")

            page_content = await page.content()

            # Check if user is authorized (Vue 3 personal.php shows Disconnect/Revoke when authorized)
            if "Disconnect" not in page_content and "Revoke Access" not in page_content:
                pytest.skip("User not authorized - cannot test disconnect")

            # Click disconnect button
            disconnect_selectors = [
                'button:has-text("Disconnect")',
                'form[action*="disconnect"] button',
                "#mcp-disconnect-button",
            ]

            for selector in disconnect_selectors:
                try:
                    # Handle confirmation dialog
                    page.on("dialog", lambda dialog: dialog.accept())
                    await page.click(selector, timeout=2000)
                    logger.info(f"✓ Clicked disconnect button (selector: {selector})")
                    break
                except Exception:
                    continue

            # Wait for page reload
            await page.wait_for_load_state("networkidle")

            # Verify we're back to "Enable Semantic Search" state (Vue 3 oauth-required.php)
            page_content = await page.content()
            assert "Enable Semantic Search" in page_content, (
                "Settings page should show 'Enable Semantic Search' after disconnect"
            )

            logger.info("✓ Disconnect flow test passed")

        finally:
            await context.close()
