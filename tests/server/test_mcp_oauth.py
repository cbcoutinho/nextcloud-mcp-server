import json
import logging
import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def test_mcp_oauth_server_connection(nc_mcp_oauth_client):
    """Test connection to OAuth-enabled MCP server."""
    result = await nc_mcp_oauth_client.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    logger.info(f"OAuth MCP server has {len(result.tools)} tools available")


async def test_mcp_oauth_tool_execution(nc_mcp_oauth_client):
    """Test executing a tool on the OAuth-enabled MCP server."""
    import json

    # Example: Execute the 'nc_notes_search_notes' tool
    result = await nc_mcp_oauth_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None
    response_data = json.loads(result.content[0].text)

    # The search response should have a 'results' field containing the list
    assert "results" in response_data
    assert isinstance(response_data["results"], list)

    logger.info(
        f"Successfully executed 'nc_notes_search_notes' tool on OAuth MCP server and got {len(response_data['results'])} notes."
    )


async def test_mcp_oauth_client_with_playwright(nc_mcp_oauth_client_playwright):
    """Test that MCP OAuth client via Playwright can execute tools."""

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
