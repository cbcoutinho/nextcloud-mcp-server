"""Smoke tests - critical path tests for quick validation.

These tests verify the most essential functionality:
- MCP server connectivity
- Basic CRUD operations for core apps
- OAuth authentication
- Tool schema validation

Run with: uv run pytest -m smoke -v
Expected runtime: ~30-60 seconds
"""

import json

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.smoke]


async def test_mcp_connectivity_smoke(nc_mcp_client):
    """Smoke test: Verify MCP server is reachable and lists tools."""
    tools = await nc_mcp_client.list_tools()

    # Should have a reasonable number of tools
    assert len(tools.tools) > 30, f"Expected >30 tools, got {len(tools.tools)}"

    # Check for core tool categories
    tool_names = [tool.name for tool in tools.tools]
    assert any("notes" in name for name in tool_names), "Missing notes tools"
    assert any("calendar" in name for name in tool_names), "Missing calendar tools"
    assert any("webdav" in name for name in tool_names), "Missing webdav tools"


async def test_notes_crud_smoke(nc_mcp_client, nc_client):
    """Smoke test: Verify basic Notes CRUD operations work."""
    # Create
    create_result = await nc_mcp_client.call_tool(
        "nc_notes_create_note",
        arguments={
            "title": "Smoke Test Note",
            "content": "Testing basic CRUD",
            "category": "test",
        },
    )
    assert create_result.isError is False
    data = json.loads(create_result.content[0].text)
    note_id = data["id"]

    try:
        # Read
        get_result = await nc_mcp_client.call_tool(
            "nc_notes_get_note",
            arguments={"note_id": note_id},
        )
        assert get_result.isError is False

        # Update
        update_result = await nc_mcp_client.call_tool(
            "nc_notes_update_note",
            arguments={
                "note_id": note_id,
                "title": "Updated Smoke Test",
                "content": "Updated content",
                "category": "test",
                "etag": data["etag"],
            },
        )
        assert update_result.isError is False

    finally:
        # Delete
        delete_result = await nc_mcp_client.call_tool(
            "nc_notes_delete_note",
            arguments={"note_id": note_id},
        )
        assert delete_result.isError is False


async def test_calendar_basic_smoke(nc_mcp_client):
    """Smoke test: Verify calendar operations work."""
    # List calendars
    result = await nc_mcp_client.call_tool(
        "nc_calendar_list_calendars",
        arguments={},
    )
    assert result.isError is False

    data = json.loads(result.content[0].text)
    assert "calendars" in data
    assert len(data["calendars"]) > 0


async def test_webdav_basic_smoke(nc_mcp_client):
    """Smoke test: Verify WebDAV file operations work."""
    # List root directory
    result = await nc_mcp_client.call_tool(
        "nc_webdav_list_directory",
        arguments={"path": "/"},
    )
    assert result.isError is False

    data = json.loads(result.content[0].text)
    assert "files" in data
    assert isinstance(data["files"], list)


@pytest.mark.oauth
async def test_oauth_connectivity_smoke(nc_mcp_oauth_client):
    """Smoke test: Verify OAuth authentication works."""
    # List tools with OAuth
    result = await nc_mcp_oauth_client.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    # Execute a simple tool
    search_result = await nc_mcp_oauth_client.call_tool(
        "nc_notes_search_notes",
        arguments={"query": ""},
    )
    assert search_result.isError is False
