"""Interactive integration tests for OAuth authentication."""

import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.interactive]


class TestOAuthInteractive:
    """Test interactive OAuth authentication."""

    async def test_mcp_oauth_tool_execution_interactive(
        self, nc_mcp_oauth_client_interactive
    ):
        """Test executing a tool on the OAuth-enabled MCP server with an interactive token."""
        # Example: Execute the 'nc_notes_list' tool
        result = await nc_mcp_oauth_client_interactive.call_tool("nc_tables_list")

        assert result.isError is False, f"Tool execution failed: {result.content}"
        assert result.content is not None
        import json

        notes_list = json.loads(result.content[0].text)

        assert isinstance(notes_list, list)

        logger.info(
            f"Successfully executed 'nc_notes_list' tool on OAuth MCP server and got {len(notes_list)} notes."
        )
