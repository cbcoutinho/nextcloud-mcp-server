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


async def authorize_search_access(page: Page, username: str) -> bool:
    """Complete Step 1: OAuth Authorization for Astrolabe.

    Handles the OAuth flow:
    1. Check if already authorized (Step 1 shows "Complete")
    2. Click "Authorize" link
    3. Handle Nextcloud OIDC consent screen
    4. Wait for redirect back to Astrolabe settings
    5. Verify "Complete" badge appears on Step 1

    Args:
        page: Playwright page instance (must be on Astrolabe settings page)
        username: Username for logging

    Returns:
        True if authorization completed successfully
    """
    nextcloud_url = "http://localhost:8080"

    logger.info(f"Authorizing search access (Step 1) for {username}...")

    # Check if already on Astrolabe settings page, if not navigate there
    if "/settings/user/astrolabe" not in page.url:
        await navigate_to_astrolabe_settings(page)

    # Wait for page to fully render
    await anyio.sleep(1)

    # Check if already authorized (either "Active" badge or Step 1 "Complete" badge)
    try:
        # Check for "Active" badge (fully configured state)
        active_badge = page.get_by_text("Active", exact=True)
        if await active_badge.count() > 0 and await active_badge.is_visible():
            logger.info(f"✓ Already fully authorized for {username} (Active badge)")
            return True
    except Exception:
        pass

    try:
        step1_section = page.locator('h4:has-text("Step 1")')
        if await step1_section.count() > 0:
            # Look for "Complete" text in the Step 1 section's parent
            step1_parent = step1_section.locator("..")
            complete_badge = step1_parent.get_by_text("Complete", exact=True)
            if await complete_badge.count() > 0 and await complete_badge.is_visible():
                logger.info(f"✓ Step 1 already complete for {username}")
                return True
    except Exception:
        pass

    # Find and click the "Authorize" button
    authorize_button = page.locator('a.button.primary:has-text("Authorize")')

    try:
        await authorize_button.wait_for(timeout=5000, state="visible")
        logger.info(f"Found Authorize button for {username}")
    except Exception:
        # Take screenshot for debugging
        screenshot_path = f"/tmp/astrolabe_no_authorize_button_{username}.png"
        await page.screenshot(path=screenshot_path)
        logger.error(
            f"Could not find Authorize button for {username}. Screenshot: {screenshot_path}"
        )
        raise ValueError(f"Authorize button not found for {username}")

    # Click the Authorize button - this will redirect to OAuth provider
    # Use force=True to bypass stability check which can timeout due to CSS transitions
    await authorize_button.click(force=True)
    logger.info(f"Clicked Authorize button for {username}")

    # Wait for OAuth redirect to complete
    await page.wait_for_load_state("networkidle", timeout=30000)
    logger.info(f"After networkidle, current URL: {page.url}")

    # Take screenshot to see current state
    await page.screenshot(path=f"/tmp/astrolabe_after_authorize_{username}.png")
    logger.info(f"Screenshot saved: /tmp/astrolabe_after_authorize_{username}.png")

    # Handle OIDC consent screen if present
    consent_handled = await _handle_oauth_consent_screen(page, username)
    if consent_handled:
        logger.info(f"✓ OAuth consent granted for {username}")
    else:
        logger.info(
            f"No consent screen required for {username} (may be previously authorized)"
        )

    # Wait for redirect back to Astrolabe settings
    # The OAuth callback will redirect back to /settings/user/astrolabe
    try:
        await page.wait_for_url(
            f"**{nextcloud_url}/settings/user/astrolabe**", timeout=30000
        )
        logger.info(f"Redirected back to Astrolabe settings for {username}")
    except Exception:
        # Check if we're already on settings page
        if "/settings/user/astrolabe" not in page.url:
            logger.warning(
                f"Not redirected to Astrolabe settings, current URL: {page.url}"
            )
            # Navigate manually
            await page.goto(
                f"{nextcloud_url}/settings/user/astrolabe", wait_until="networkidle"
            )

    # Wait for page to reload and render
    await anyio.sleep(2)

    # Verify authorization completed - check for various success indicators
    # When fully configured, shows "Active" badge; when only Step 1 done, shows "Complete"
    try:
        # First check if "Active" badge is shown (fully configured state)
        active_badge = page.get_by_text("Active", exact=True)
        if await active_badge.count() > 0 and await active_badge.is_visible():
            logger.info(f"✓ OAuth authorization complete for {username} (Active badge)")
            return True
    except Exception:
        pass

    try:
        # Check for Step 1 "Complete" badge (partial configuration)
        step1_section = page.locator('h4:has-text("Step 1")')
        if await step1_section.count() > 0:
            step1_parent = step1_section.locator("..")
            complete_badge = step1_parent.get_by_text("Complete", exact=True)
            await complete_badge.wait_for(timeout=5000, state="visible")
            logger.info(f"✓ Step 1 OAuth authorization complete for {username}")
            return True
    except Exception:
        pass

    # Neither badge found - authorization failed
    screenshot_path = f"/tmp/astrolabe_step1_not_complete_{username}.png"
    await page.screenshot(path=screenshot_path)
    logger.error(
        f"Authorization badge not visible for {username}. Screenshot: {screenshot_path}"
    )
    raise ValueError(f"OAuth authorization did not complete for {username}")


async def _handle_oauth_consent_screen(page: Page, username: str) -> bool:
    """Handle the OIDC consent screen during OAuth flow.

    Reuses the proven pattern from tests/conftest.py.

    Args:
        page: Playwright page instance
        username: Username for logging

    Returns:
        True if consent was handled, False if no consent screen was found
    """
    try:
        logger.info(f"Checking for consent screen at URL: {page.url}")

        # Check if consent screen is present - try multiple selectors
        # The consent screen may be #oidc-consent or use a different format
        consent_div = await page.query_selector("#oidc-consent")

        if consent_div:
            logger.info(f"Consent screen detected via #oidc-consent for {username}")
            # Get consent screen data attributes for logging
            client_name = await consent_div.get_attribute("data-client-name")
            scopes_attr = await consent_div.get_attribute("data-scopes")
            logger.info(f"  Client: {client_name}")
            logger.info(f"  Requested scopes: {scopes_attr}")
        else:
            # Check for Allow button directly (different consent screen format)
            allow_button = page.locator('button:has-text("Allow")')
            if await allow_button.count() > 0:
                logger.info(f"Consent screen detected via Allow button for {username}")
            else:
                logger.info(f"No consent screen found for {username} at {page.url}")
                await page.screenshot(path=f"/tmp/no_consent_screen_{username}.png")
                logger.info(f"Screenshot: /tmp/no_consent_screen_{username}.png")
                return False

        # Wait for Vue.js to render the Allow button
        try:
            await page.wait_for_selector('button:has-text("Allow")', timeout=10000)
            logger.info("  Allow button rendered by Vue.js")
        except Exception as e:
            screenshot_path = f"/tmp/consent_no_allow_button_{username}.png"
            await page.screenshot(path=screenshot_path)
            logger.error(f"  Timeout waiting for Allow button: {e}")
            raise

        # Check all scope checkboxes
        scope_checkboxes = await page.query_selector_all('input[type="checkbox"]')
        if scope_checkboxes:
            logger.info(f"  Found {len(scope_checkboxes)} scope checkboxes")
            for i, checkbox in enumerate(scope_checkboxes):
                is_checked = await checkbox.is_checked()
                is_disabled = await checkbox.is_disabled()
                if not is_checked and not is_disabled:
                    await checkbox.check()
                    logger.info(f"    ✓ Checked scope checkbox {i + 1}")

        # Click the Allow button using JavaScript (handles viewport issues)
        allow_button_locator = page.locator('button:has-text("Allow")')

        # Debug: take screenshot before clicking Allow
        await page.screenshot(path=f"/tmp/consent_before_allow_{username}.png")
        logger.info(
            f"  Screenshot before Allow: /tmp/consent_before_allow_{username}.png"
        )

        button_count = await allow_button_locator.count()
        logger.info(f"  Found {button_count} Allow button(s)")

        if button_count > 0:
            current_url = page.url
            logger.info(f"  Current URL: {current_url}")
            logger.info(f"  Clicking Allow button for {username}...")

            # Use JavaScript click to handle consent buttons (proven pattern from conftest.py)
            # This is more reliable than Playwright's click for Vue.js rendered buttons
            await page.evaluate(
                """
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    if (btn.textContent.trim() === 'Allow') {
                        btn.click();
                        break;
                    }
                }
                """
            )

            # Wait for URL to change (Vue.js uses window.location.href after fetch)
            # networkidle doesn't detect fetch-based redirects
            try:
                await page.wait_for_url(
                    lambda url: url != current_url,
                    timeout=30000,
                )
                logger.info(f"  URL changed to: {page.url}")
            except Exception as wait_error:
                # If URL didn't change, check console for errors
                logger.warning(f"  URL didn't change after click: {wait_error}")
                await page.screenshot(path=f"/tmp/consent_after_allow_{username}.png")

                # Try alternative: manually POST consent and navigate
                logger.info("  Trying manual consent submission...")
                try:
                    redirect_url = await page.evaluate(
                        """
                        async () => {
                            const selectedScopes = Array.from(document.querySelectorAll('input[type="checkbox"]:checked'))
                                .map(cb => cb.value).join(' ');

                            const response = await fetch('/index.php/apps/oidc/consent/grant', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/x-www-form-urlencoded',
                                    'requesttoken': OC.requestToken,
                                },
                                body: 'scopes=' + encodeURIComponent(selectedScopes),
                                redirect: 'follow',
                            });

                            return response.url || '/index.php/apps/oidc/authorize';
                        }
                        """
                    )
                    logger.info(f"  Manual consent returned URL: {redirect_url}")
                    await page.goto(redirect_url, wait_until="networkidle")
                except Exception as manual_error:
                    logger.error(f"  Manual consent also failed: {manual_error}")
                    raise

            await page.screenshot(path=f"/tmp/consent_after_allow_{username}.png")
            logger.info(f"  Consent granted for {username}")
            return True
        else:
            logger.error(f"  Allow button not found for {username}")
            return False

    except Exception as e:
        logger.error(f"Error handling consent screen for {username}: {e}")
        raise


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

    # Click the create button - use force=True to bypass stability check (CSS transitions)
    create_button = page.locator(
        'button[type="submit"]:has-text("Create new app password")'
    )
    try:
        await create_button.click(force=True, timeout=10000)
    except Exception:
        # Fallback: JavaScript click
        logger.info("Using JavaScript click for create button...")
        await page.evaluate(
            """
            const btn = document.querySelector('button[type="submit"]');
            if (btn) btn.click();
            """
        )
    logger.info("Clicked create app password button")

    # Wait for app password to be generated and displayed in the dialog
    await anyio.sleep(3)  # Give it more time to generate and display

    # Debug screenshot after clicking create
    await page.screenshot(path=f"/tmp/app_password_after_create_{username}.png")
    logger.info(
        f"Screenshot after create: /tmp/app_password_after_create_{username}.png"
    )

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

    # Close dialog with Escape key (bypasses CSS layout issues with h2 intercepting clicks)
    logger.info("Closing app password dialog with Escape key...")
    await page.keyboard.press("Escape")
    await anyio.sleep(0.5)  # Wait for dialog close animation
    logger.info("Closed app password dialog")

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

    # Check if already complete (look for Step 2 "Complete" badge or overall "Active" state)
    try:
        # First check for overall "Active" badge (both steps complete)
        active_text = page.get_by_text("Active", exact=True)
        if await active_text.is_visible(timeout=2000):
            logger.info(f"✓ Background sync already active for {username}")
            return True
    except Exception:
        pass

    try:
        # Check for Step 2 "Complete" badge (app password already set)
        step2_section = page.locator('h4:has-text("Step 2")')
        if await step2_section.count() > 0:
            step2_parent = step2_section.locator("..")
            complete_badge = step2_parent.get_by_text("Complete", exact=True)
            if await complete_badge.count() > 0 and await complete_badge.is_visible():
                logger.info(f"✓ Step 2 (app password) already complete for {username}")
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

    # Verify Step 2 "Complete" badge or overall "Active" badge appears after reload
    try:
        # First try to find "Active" badge (both steps complete)
        active_text = page.get_by_text("Active", exact=True)
        if await active_text.count() > 0:
            await active_text.wait_for(timeout=5000, state="visible")
            logger.info(
                f"✓ Background sync enabled for {username} - Active badge visible"
            )
            return True
    except Exception:
        pass

    try:
        # Check for Step 2 "Complete" badge
        step2_section = page.locator('h4:has-text("Step 2")')
        if await step2_section.count() > 0:
            step2_parent = step2_section.locator("..")
            complete_badge = step2_parent.get_by_text("Complete", exact=True)
            await complete_badge.wait_for(timeout=5000, state="visible")
            logger.info(
                f"✓ Step 2 (app password) enabled for {username} - Complete badge visible"
            )
            return True
    except Exception:
        pass

    # If neither badge found, raise error
    screenshot_path = f"/tmp/astrolabe_after_password_{username}.png"
    await page.screenshot(path=screenshot_path)
    logger.error(
        f"Neither Active nor Complete badge appeared for {username}. "
        f"Screenshot: {screenshot_path}"
    )
    raise ValueError(f"Background sync setup did not complete for {username}")


async def complete_astrolabe_authorization(
    page: Page, username: str, password: str
) -> dict:
    """Complete full Astrolabe two-step authorization.

    Performs the complete authorization flow:
    1. Navigate to Astrolabe settings
    2. OAuth authorization (Step 1) if needed
    3. Generate app password in Security settings
    4. App password entry (Step 2) if needed

    Args:
        page: Playwright page instance (must be logged in)
        username: Nextcloud username
        password: Nextcloud password (for reference, not used directly)

    Returns:
        Dict with {"step1": bool, "step2": bool, "app_password": str | None}
    """
    logger.info(f"Starting full Astrolabe authorization for {username}...")

    result = {"step1": False, "step2": False, "app_password": None}

    # Navigate to Astrolabe settings
    await navigate_to_astrolabe_settings(page)

    # Step 1: OAuth authorization
    try:
        result["step1"] = await authorize_search_access(page, username)
        logger.info(f"✓ Step 1 complete for {username}")
    except Exception as e:
        logger.error(f"Step 1 failed for {username}: {e}")
        raise

    # Navigate back to settings if needed (OAuth might have redirected elsewhere)
    if "/settings/user/astrolabe" not in page.url:
        await navigate_to_astrolabe_settings(page)

    # Check if Step 2 is already complete
    try:
        step2_section = page.locator('h4:has-text("Step 2")')
        if await step2_section.count() > 0:
            step2_parent = step2_section.locator("..")
            complete_badge = step2_parent.get_by_text("Complete", exact=True)
            if await complete_badge.count() > 0 and await complete_badge.is_visible():
                logger.info(f"✓ Step 2 already complete for {username}")
                result["step2"] = True
                return result
    except Exception:
        pass

    # Also check for overall "Active" badge
    try:
        active_text = page.get_by_text("Active", exact=True)
        if await active_text.count() > 0 and await active_text.is_visible():
            logger.info(f"✓ Authorization already fully active for {username}")
            result["step2"] = True
            return result
    except Exception:
        pass

    # Step 2: Generate app password and enter it
    app_password = await generate_app_password(page, username)
    result["app_password"] = app_password

    try:
        result["step2"] = await enable_background_sync_via_app_password(
            page, username, app_password
        )
        logger.info(f"✓ Step 2 complete for {username}")
    except Exception as e:
        logger.error(f"Step 2 failed for {username}: {e}")
        raise

    logger.info(f"✓ Full Astrolabe authorization complete for {username}")
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


async def revoke_background_sync_access(page: Page, username: str) -> bool:
    """Revoke background sync access by clicking the Revoke Access button.

    Args:
        page: Playwright page instance (must be authenticated)
        username: Username (for logging)

    Returns:
        True if revocation was successful
    """
    logger.info(f"Revoking background sync access for {username}...")

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

    # Check if "Active" badge is visible (indicating background sync is enabled)
    try:
        active_text = page.get_by_text("Active", exact=True)
        if not await active_text.is_visible(timeout=2000):
            logger.warning(
                f"Background sync not active for {username}, nothing to revoke"
            )
            return False
    except Exception:
        logger.warning(f"Could not find Active badge for {username}")
        return False

    # Find the "Revoke Access" button
    revoke_button = page.get_by_role("button", name="Revoke Access")

    try:
        await revoke_button.wait_for(timeout=5000, state="visible")
        logger.info("Found Revoke Access button")
    except Exception:
        screenshot_path = f"/tmp/astrolabe_no_revoke_button_{username}.png"
        await page.screenshot(path=screenshot_path)
        raise ValueError(
            f"Could not find Revoke Access button for {username}. Screenshot: {screenshot_path}"
        )

    # Set up dialog handler for confirmation dialog
    page.once("dialog", lambda dialog: dialog.accept())

    # Click the Revoke Access button
    await revoke_button.click()
    logger.info("Clicked Revoke Access button")

    # Wait for the request to complete and page to reload
    await page.wait_for_load_state("networkidle", timeout=15000)
    await anyio.sleep(2)

    # Log network requests after clicking
    logger.info(f"Network requests after Revoke for {username}:")
    for req in network_requests[-10:]:
        logger.info(f"  {req}")

    # Log network responses
    logger.info(f"Network responses after Revoke for {username}:")
    for resp in network_responses[-10:]:
        logger.info(f"  {resp}")

    # Check specifically for the revoke POST response
    revoke_responses = [r for r in network_responses if "credentials/revoke" in r]
    if revoke_responses:
        logger.info(f"Revoke endpoint response: {revoke_responses[-1]}")
        if "200" not in revoke_responses[-1]:
            logger.error(f"Revoke POST did not return 200 OK: {revoke_responses[-1]}")
            return False
    else:
        logger.warning("No response found for credentials/revoke endpoint!")
        # Take screenshot for debugging
        screenshot_path = f"/tmp/astrolabe_revoke_no_response_{username}.png"
        await page.screenshot(path=screenshot_path)
        return False

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
            return False
    except Exception:
        pass

    # Verify "Active" badge is no longer visible
    try:
        active_text = page.get_by_text("Active", exact=True)
        if await active_text.is_visible(timeout=2000):
            logger.error(f"Active badge still visible for {username} after revoke!")
            screenshot_path = f"/tmp/astrolabe_revoke_still_active_{username}.png"
            await page.screenshot(path=screenshot_path)
            return False
    except Exception:
        pass

    logger.info(f"✓ Background sync access revoked for {username}")
    return True


async def verify_app_password_deleted(username: str) -> bool:
    """Verify that background sync app password was deleted for the user.

    Args:
        username: Nextcloud username

    Returns:
        True if background sync credentials no longer exist
    """
    logger.info(f"Verifying background sync credentials deleted for {username}...")

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

        # After deletion, we should NOT see background_sync_password
        if "background_sync_password" not in output:
            logger.info(f"✓ Background sync credentials deleted for {username}")
            return True
        else:
            logger.warning(f"Background sync credentials still exist for {username}")
            return False

    except Exception as e:
        logger.error(f"Error checking background sync credentials for {username}: {e}")
        return False


@pytest.mark.integration
@pytest.mark.oauth
async def test_revoke_background_sync_access(
    browser,
    nc_client,
    test_users_setup,
    configure_astrolabe_for_mcp_server,
):
    """Test that users can revoke background sync access via the Revoke Access button.

    This test verifies:
    1. User enables background sync via app password
    2. User clicks "Revoke Access" button
    3. Confirmation dialog is handled
    4. POST request is sent to /api/v1/background-sync/credentials/revoke
    5. "Active" badge disappears from settings page
    6. Background sync credentials are deleted from database

    This tests the fix for the issue where POST requests to the revoke endpoint
    were returning errors due to HTTP method mismatch (was DELETE, now POST).
    """
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

        # Step 2: Generate app password and enable background sync
        app_password = await generate_app_password(page, username)
        await enable_background_sync_via_app_password(page, username, app_password)

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

        logger.info(f"\n✓ Successfully revoked background sync access for {username}!")

    finally:
        await context.close()
