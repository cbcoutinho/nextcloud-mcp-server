"""Debug test to capture what's on the NC PHP app settings page."""

import logging
import os

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def test_capture_settings_page(browser):
    """Capture what's actually rendered on the personal settings page."""
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
        await page.goto(f"{nextcloud_host}/settings/user/mcp")
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

        # Check for key strings
        checks = [
            "Authorize Access",
            "Authorization Required",
            "MCP Server",
            "Sign In Again",
            "astroglobe",
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

        # Check for error messages
        if "error" in page_content.lower():
            logger.warning("Page contains 'error' keyword")

    finally:
        await context.close()
