"""Integration tests for Playwright-based OAuth authentication."""

import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


class TestOAuthPlaywright:
    """Test automated Playwright OAuth authentication."""

    async def test_playwright_oauth_token_acquisition(
        self, playwright_oauth_token: str
    ):
        """Test that Playwright can acquire an OAuth token automatically."""
        assert playwright_oauth_token is not None
        assert isinstance(playwright_oauth_token, str)
        assert len(playwright_oauth_token) > 0
        logger.info(
            f"Successfully acquired OAuth token via Playwright: {playwright_oauth_token[:20]}..."
        )

    async def test_oauth_client_with_playwright_flow(self, nc_oauth_client_playwright):
        """Test that OAuth client created via Playwright flow can access Nextcloud APIs."""
        # Test 1: Check capabilities
        capabilities = await nc_oauth_client_playwright.capabilities()
        assert capabilities is not None
        logger.info("OAuth client (Playwright) successfully fetched capabilities")

        # Test 2: List notes
        notes = await nc_oauth_client_playwright.notes.get_all_notes()
        assert isinstance(notes, list)
        logger.info(f"OAuth client (Playwright) successfully listed {len(notes)} notes")

    async def test_mcp_oauth_client_with_playwright(
        self, nc_mcp_oauth_client_playwright
    ):
        """Test that MCP OAuth client via Playwright can execute tools."""
        import json

        # Test: Execute the 'nc_notes_search_notes' tool
        result = await nc_mcp_oauth_client_playwright.call_tool(
            "nc_notes_search_notes", arguments={"query": ""}
        )

        assert result.isError is False, f"Tool execution failed: {result.content}"
        assert result.content is not None
        response_data = json.loads(result.content[0].text)

        # The search response should have a 'results' field containing the list
        assert "results" in response_data
        assert isinstance(response_data["results"], list)

        logger.info(
            f"Successfully executed 'nc_notes_search_notes' tool on Playwright OAuth MCP server and got {len(response_data['results'])} notes."
        )
