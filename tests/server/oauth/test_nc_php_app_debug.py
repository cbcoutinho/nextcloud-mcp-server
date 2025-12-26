"""Debug test to capture what's on the NC PHP app settings page."""

import logging
import os

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def test_capture_settings_page(browser, configure_astrolabe_for_mcp_server):
    """Capture what's actually rendered on the personal settings page."""
    # Configure Astrolabe for mcp-oauth server
    await configure_astrolabe_for_mcp_server(
        mcp_server_internal_url="http://mcp-oauth:8001",
        mcp_server_public_url="http://localhost:8001",
    )

    nextcloud_host = os.getenv("NEXTCLOUD_HOST", "http://localhost:8080")
    username = os.getenv("NEXTCLOUD_USERNAME", "admin")
    password = os.getenv("NEXTCLOUD_PASSWORD", "admin")

    context = await browser.new_context()
    page = await context.new_page()

    try:
        # Login
        logger.info(f"Logging in to {nextcloud_host} as {username}...")
        await page.goto(f"{nextcloud_host}/login")
        await page.fill('input[name="user"]', username)
        await page.fill('input[name="password"]', password)
        await page.click('button[type="submit"]')
        await page.wait_for_url(f"{nextcloud_host}/apps/dashboard/", timeout=10000)
        logger.info("✓ Logged in")

        # Navigate to settings
        logger.info("Navigating to personal MCP settings...")
        await page.goto(f"{nextcloud_host}/settings/user/astrolabe")
        await page.wait_for_load_state("networkidle")

        # Capture page content
        page_content = await page.content()

        # Save screenshot
        screenshot_path = "/tmp/nc-php-app-settings-debug.png"
        await page.screenshot(path=screenshot_path, full_page=True)
        logger.info(f"Screenshot saved to: {screenshot_path}")

        # Log what we found
        logger.info(f"Page URL: {page.url}")
        logger.info(f"Page title: {await page.title()}")

        # Check for key strings (Vue 3 UI)
        checks = [
            "Enable Semantic Search",  # oauth-required.php authorization button
            "Service Status",  # personal.php when authorized
            "Background Sync Access",  # personal.php when authorized
            "What happens next?",  # oauth-required.php steps
            "Astrolabe",  # Header
        ]

        for check in checks:
            found = check in page_content
            logger.info(f"  '{check}': {'FOUND' if found else 'NOT FOUND'}")

        # Print first 500 chars of body
        body = await page.locator("body").text_content()
        logger.info(f"Body text (first 500 chars): {body[:500] if body else 'NO BODY'}")

        # Try to find links
        links = await page.locator("a").all_text_contents()
        logger.info(f"Found {len(links)} links on page")
        for i, link_text in enumerate(links[:10]):
            logger.info(f"  Link {i}: {link_text}")

        # Check the Enable Semantic Search button href
        try:
            btn = page.locator('a:has-text("Enable Semantic Search")')
            if await btn.count() > 0:
                href = await btn.get_attribute("href")
                logger.info(f"Enable Semantic Search button href: {href}")
        except Exception as e:
            logger.warning(f"Could not get button href: {e}")

        # Check for error messages
        if "error" in page_content.lower():
            logger.warning("Page contains 'error' keyword")

    finally:
        await context.close()
