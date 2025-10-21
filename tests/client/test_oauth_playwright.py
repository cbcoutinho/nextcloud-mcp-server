"""Integration tests for Playwright-based OAuth authentication."""

import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def test_playwright_oauth_token_acquisition(playwright_oauth_token: str):
    """Test that Playwright can acquire an OAuth token automatically."""
    assert playwright_oauth_token is not None
    assert isinstance(playwright_oauth_token, str)
    assert len(playwright_oauth_token) > 0
    logger.info(
        f"Successfully acquired OAuth token via Playwright: {playwright_oauth_token[:20]}..."
    )


async def test_oauth_client_with_playwright_flow(nc_oauth_client):
    """Test that OAuth client created via Playwright flow can access Nextcloud APIs."""
    # Test 1: Check capabilities
    capabilities = await nc_oauth_client.capabilities()
    assert capabilities is not None
    logger.info("OAuth client (Playwright) successfully fetched capabilities")

    # Test 2: List notes
    notes = [note async for note in nc_oauth_client.notes.get_all_notes()]
    assert isinstance(notes, list)
    logger.info(f"OAuth client (Playwright) successfully listed {len(notes)} notes")
