"""Test error propagation in the MCP server for various error scenarios."""

import logging

import pytest
from mcp import ClientSession

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


async def test_missing_note_tool_error(nc_mcp_client: ClientSession):
    """Test that accessing a non-existent note via tool returns proper error."""
    # Try to get a non-existent note via tool - should return error response
    response = await nc_mcp_client.call_tool("nc_notes_get_note", {"note_id": 999999})

    # Should return error response (not raise exception) for tools
    assert response is not None
    assert response.is_error is True
    assert "Note 999999 not found" in response.content[0].text


async def test_delete_missing_note_tool_error(nc_mcp_client: ClientSession):
    """Test that deleting a non-existent note returns proper error."""
    # Try to delete a non-existent note - should return error response
    response = await nc_mcp_client.call_tool(
        "nc_notes_delete_note", {"note_id": 999999}
    )

    # Should return error response (not raise exception) for tools
    assert response is not None
    assert response.is_error is True
    assert "Note 999999 not found" in response.content[0].text


async def test_missing_file_error_is_llm_friendly(nc_mcp_client: ClientSession):
    """A tool with no bespoke handler still returns actionable text (GH #1208).

    Guards the ``NextcloudMCPServer`` boundary: httpx's raw
    ``Client error '404 Not Found' for url 'http://app/remote.php/dav/...'``
    plus an MDN link must not reach the model.
    """
    response = await nc_mcp_client.call_tool(
        "nc_webdav_read_file", {"path": "FileUpload/does-not-exist.txtg"}
    )

    assert response is not None
    assert response.is_error is True
    text = response.content[0].text
    logger.info("Missing file response: %s", text)

    assert "FileUpload/does-not-exist.txtg" in text
    assert "Not found" in text
    # No internal routing, no host, no MDN link.
    assert "remote.php" not in text
    assert "developer.mozilla.org" not in text


async def test_search_with_empty_query(nc_mcp_client: ClientSession):
    """Test search behavior with empty query."""
    # Search with empty query
    response = await nc_mcp_client.call_tool("nc_notes_search_notes", {"query": ""})

    logger.info("Empty search query response: %s", response)

    # Should return successful response with empty or valid results
    assert response is not None
    assert response.is_error is False


async def test_tool_missing_required_parameters(nc_mcp_client: ClientSession):
    """Test calling a tool with missing required parameters."""
    # Try to create note with missing parameters
    response = await nc_mcp_client.call_tool(
        "nc_notes_create_note",
        {"title": "Test"},  # Missing content and category
    )
    logger.info("Missing params response: %s", response)

    # Should return error response for missing required parameters
    assert response is not None
    assert response.is_error is True
    assert (
        "required" in response.content[0].text.lower()
        or "missing" in response.content[0].text.lower()
    )


async def test_update_note_with_invalid_etag(nc_mcp_client: ClientSession, nc_client):
    """Test updating a note with invalid ETag."""
    # First create a note
    note_data = await nc_client.notes.create_note(
        title="Test Note for ETag", content="Test content", category=""
    )
    note_id = note_data["id"]

    try:
        # Try to update with invalid ETag - should return error response
        response = await nc_mcp_client.call_tool(
            "nc_notes_update_note",
            {
                "note_id": note_id,
                "etag": "invalid-etag",
                "title": "Updated Title",
                "content": None,
                "category": None,
            },
        )

        # Should return error response (not raise exception) for tools
        assert response is not None
        assert response.is_error is True
        assert "modified by someone else" in response.content[0].text

    finally:
        # Clean up
        await nc_client.notes.delete_note(note_id)


async def test_calendar_missing_calendar_error(nc_mcp_client: ClientSession):
    """Test calendar operations with non-existent calendar."""
    # Try to create event in non-existent calendar
    response = await nc_mcp_client.call_tool(
        "nc_calendar_create_event",
        {
            "calendar_name": "non-existent-calendar",
            "title": "Test Event",
            "start_datetime": "2025-01-15T14:00:00",
        },
    )

    logger.info("Non-existent calendar response: %s", response)

    # Should return structured error response
    assert response is not None
    # Note: Some modules may not have improved error handling yet
    # Check if we have structured content with success=false or isError=true
    if (
        hasattr(response, "structuredContent")
        and response.structured_content
        and "result" in response.structured_content
    ):
        assert response.structured_content["result"]["success"] is False
    else:
        assert response.is_error is True


async def test_webdav_read_missing_file_error(nc_mcp_client: ClientSession):
    """Test WebDAV operations with non-existent file."""
    # Try to read a non-existent file
    response = await nc_mcp_client.call_tool(
        "nc_webdav_read_file", {"path": "non-existent-file.txt"}
    )

    logger.info("Missing file response: %s", response)

    # Should return structured error response
    assert response is not None
    # Note: Some modules may not have improved error handling yet
    # Check if we have structured content with success=false or isError=true
    if (
        hasattr(response, "structuredContent")
        and response.structured_content
        and "result" in response.structured_content
    ):
        assert response.structured_content["result"]["success"] is False
    else:
        assert response.is_error is True


async def test_tables_missing_table_error(nc_mcp_client: ClientSession):
    """Test Tables operations with non-existent table."""
    # Try to get schema of non-existent table
    response = await nc_mcp_client.call_tool(
        "nc_tables_get_schema", {"table_id": 999999}
    )

    logger.info("Missing table response: %s", response)

    # Should return structured error response
    assert response is not None
    # Note: Some modules may not have improved error handling yet
    # Check if we have structured content with success=false or isError=true
    if (
        hasattr(response, "structuredContent")
        and response.structured_content
        and "result" in response.structured_content
    ):
        assert response.structured_content["result"]["success"] is False
    else:
        assert response.is_error is True


async def test_calendar_list_events_requires_calendar_name(
    nc_mcp_client: ClientSession,
):
    """calendar_name is optional in the schema but still required in practice.

    Omitting it without search_all_calendars must fail loudly rather than
    silently searching a default calendar.
    """
    response = await nc_mcp_client.call_tool("nc_calendar_list_events", {})

    assert response.is_error is True
    assert "calendar_name is required" in str(response.content)


async def test_calendar_list_events_calendar_name_not_schema_required(
    nc_mcp_client: ClientSession,
):
    """The whole point of the change: MCP clients must not be forced to invent a
    throwaway calendar_name when searching every calendar."""
    tools = await nc_mcp_client.list_tools()
    tool = next(t for t in tools.tools if t.name == "nc_calendar_list_events")

    assert "calendar_name" not in (tool.input_schema.get("required") or [])
