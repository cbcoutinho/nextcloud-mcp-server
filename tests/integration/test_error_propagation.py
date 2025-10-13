"""Test error propagation in the MCP server for various error scenarios."""

import logging

import pytest
from mcp import ClientSession

logger = logging.getLogger(__name__)


@pytest.mark.integration
async def test_missing_note_tool_error(nc_mcp_client: ClientSession):
    """Test that accessing a non-existent note via tool returns proper error."""
    # Try to get a non-existent note via tool - should return error response
    response = await nc_mcp_client.call_tool("nc_notes_get_note", {"note_id": 999999})

    # Should return error response (not raise exception) for tools
    assert response is not None
    assert response.isError is True
    assert "Note 999999 not found" in response.content[0].text


@pytest.mark.integration
async def test_delete_missing_note_tool_error(nc_mcp_client: ClientSession):
    """Test that deleting a non-existent note returns proper error."""
    # Try to delete a non-existent note - should return error response
    response = await nc_mcp_client.call_tool(
        "nc_notes_delete_note", {"note_id": 999999}
    )

    # Should return error response (not raise exception) for tools
    assert response is not None
    assert response.isError is True
    assert "Note 999999 not found" in response.content[0].text


@pytest.mark.integration
async def test_search_with_empty_query(nc_mcp_client: ClientSession):
    """Test search behavior with empty query."""
    # Search with empty query
    response = await nc_mcp_client.call_tool("nc_notes_search_notes", {"query": ""})

    logger.info(f"Empty search query response: {response}")

    # Should return successful response with empty or valid results
    assert response is not None
    assert response.isError is False


@pytest.mark.integration
async def test_tool_missing_required_parameters(nc_mcp_client: ClientSession):
    """Test calling a tool with missing required parameters."""
    # Try to create note with missing parameters
    response = await nc_mcp_client.call_tool(
        "nc_notes_create_note",
        {"title": "Test"},  # Missing content and category
    )
    logger.info(f"Missing params response: {response}")

    # Should return error response for missing required parameters
    assert response is not None
    assert response.isError is True
    assert (
        "required" in response.content[0].text.lower()
        or "missing" in response.content[0].text.lower()
    )


@pytest.mark.integration
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
        assert response.isError is True
        assert "modified by someone else" in response.content[0].text

    finally:
        # Clean up
        await nc_client.notes.delete_note(note_id)


@pytest.mark.integration
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

    logger.info(f"Non-existent calendar response: {response}")

    # Should return structured error response
    assert response is not None
    # Note: Some modules may not have improved error handling yet
    # Check if we have structured content with success=false or isError=true
    if (
        hasattr(response, "structuredContent")
        and response.structuredContent
        and "result" in response.structuredContent
    ):
        assert response.structuredContent["result"]["success"] is False
    else:
        assert response.isError is True


@pytest.mark.integration
async def test_webdav_read_missing_file_error(nc_mcp_client: ClientSession):
    """Test WebDAV operations with non-existent file."""
    # Try to read a non-existent file
    response = await nc_mcp_client.call_tool(
        "nc_webdav_read_file", {"path": "non-existent-file.txt"}
    )

    logger.info(f"Missing file response: {response}")

    # Should return structured error response
    assert response is not None
    # Note: Some modules may not have improved error handling yet
    # Check if we have structured content with success=false or isError=true
    if (
        hasattr(response, "structuredContent")
        and response.structuredContent
        and "result" in response.structuredContent
    ):
        assert response.structuredContent["result"]["success"] is False
    else:
        assert response.isError is True


@pytest.mark.integration
async def test_tables_missing_table_error(nc_mcp_client: ClientSession):
    """Test Tables operations with non-existent table."""
    # Try to get schema of non-existent table
    response = await nc_mcp_client.call_tool(
        "nc_tables_get_schema", {"table_id": 999999}
    )

    logger.info(f"Missing table response: {response}")

    # Should return structured error response
    assert response is not None
    # Note: Some modules may not have improved error handling yet
    # Check if we have structured content with success=false or isError=true
    if (
        hasattr(response, "structuredContent")
        and response.structuredContent
        and "result" in response.structuredContent
    ):
        assert response.structuredContent["result"]["success"] is False
    else:
        assert response.isError is True
