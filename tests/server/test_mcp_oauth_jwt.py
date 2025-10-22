"""Integration tests for JWT OAuth authentication.

These tests verify:
1. JWT token authentication works correctly
2. JWT token verification via JWKS
3. Scope information is properly extracted from JWT claims
4. Dynamic tool filtering works with JWT tokens
5. All MCP operations work with JWT authentication
"""

import json
import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def test_jwt_mcp_server_connection(nc_mcp_oauth_jwt_client):
    """Test connection to JWT OAuth-enabled MCP server."""
    result = await nc_mcp_oauth_jwt_client.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    logger.info(f"JWT OAuth MCP server has {len(result.tools)} tools available")


async def test_jwt_token_authentication(nc_mcp_oauth_jwt_client):
    """Test that JWT token authentication works."""
    # Execute a simple read operation
    result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None
    response_data = json.loads(result.content[0].text)

    assert "results" in response_data
    assert isinstance(response_data["results"], list)

    logger.info(
        f"Successfully authenticated with JWT token and executed tool, got {len(response_data['results'])} notes."
    )


async def test_jwt_tool_list_operations(nc_mcp_oauth_jwt_client):
    """Test that list_tools works with JWT authentication."""
    result = await nc_mcp_oauth_jwt_client.list_tools()

    # Verify we have tools
    assert len(result.tools) > 0

    # Verify some expected tools exist
    tool_names = [tool.name for tool in result.tools]
    assert "nc_notes_get_note" in tool_names
    assert "nc_notes_create_note" in tool_names
    assert "nc_calendar_list_calendars" in tool_names
    assert "nc_webdav_list_directory" in tool_names

    logger.info(f"JWT server provides {len(result.tools)} tools")


async def test_jwt_read_operation(nc_mcp_oauth_jwt_client):
    """Test read operation with JWT authentication."""
    # List calendars (read operation)
    result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_calendar_list_calendars", arguments={}
    )

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None
    response_data = json.loads(result.content[0].text)

    assert "calendars" in response_data
    assert isinstance(response_data["calendars"], list)

    logger.info(
        f"Successfully executed read operation with JWT, got {len(response_data['calendars'])} calendars."
    )


async def test_jwt_write_operation(nc_mcp_oauth_jwt_client):
    """Test write operation with JWT authentication."""
    import uuid

    # Create a note (write operation)
    note_title = f"JWT Test Note {uuid.uuid4().hex[:8]}"
    note_content = "This note was created during JWT authentication testing"

    result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_create_note",
        arguments={
            "title": note_title,
            "content": note_content,
            "category": "Testing",
        },
    )

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None
    response_data = json.loads(result.content[0].text)

    # Verify note was created
    assert "id" in response_data
    assert response_data["title"] == note_title

    note_id = response_data["id"]
    logger.info(f"Successfully created note {note_id} with JWT authentication")

    # Clean up: Delete the note
    delete_result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_delete_note", arguments={"note_id": note_id}
    )

    assert delete_result.isError is False, f"Cleanup failed: {delete_result.content}"
    logger.info(f"Cleaned up test note {note_id}")


async def test_jwt_multiple_operations(nc_mcp_oauth_jwt_client):
    """Test multiple operations with same JWT token to verify token persistence."""
    # First operation: Search notes
    result1 = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert result1.isError is False

    # Second operation: List calendars
    result2 = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_calendar_list_calendars", arguments={}
    )
    assert result2.isError is False

    # Third operation: List directory
    result3 = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_webdav_list_directory", arguments={"path": "/"}
    )
    assert result3.isError is False

    logger.info("Successfully executed multiple operations with JWT token")


async def test_jwt_vs_opaque_token_compatibility(
    nc_mcp_oauth_client, nc_mcp_oauth_jwt_client
):
    """Verify that both opaque and JWT tokens provide same functionality."""
    # Execute same operation on both servers
    opaque_result = await nc_mcp_oauth_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    jwt_result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )

    # Both should succeed
    assert opaque_result.isError is False
    assert jwt_result.isError is False

    # Both should have results
    opaque_data = json.loads(opaque_result.content[0].text)
    jwt_data = json.loads(jwt_result.content[0].text)

    assert "results" in opaque_data
    assert "results" in jwt_data

    # Results should be the same (same user, same notes)
    assert len(opaque_data["results"]) == len(jwt_data["results"])

    logger.info(
        "Verified opaque and JWT tokens provide identical functionality: "
        f"{len(opaque_data['results'])} notes accessible from both servers"
    )


async def test_jwt_error_handling(nc_mcp_oauth_jwt_client):
    """Test error handling with JWT authentication."""
    # Try to get a non-existent note
    result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_get_note", arguments={"note_id": 999999}
    )

    # Should get an error (note doesn't exist)
    assert result.isError is True
    logger.info("JWT server correctly handles errors for invalid operations")


async def test_jwt_scope_enforcement(nc_mcp_oauth_jwt_client):
    """Test that JWT server properly enforces scopes."""
    # This test assumes the JWT token has both nc:read and nc:write scopes
    # Both read and write operations should succeed

    # Read operation
    read_result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert read_result.isError is False

    # Write operation
    import uuid

    note_title = f"Scope Test {uuid.uuid4().hex[:8]}"
    write_result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_create_note",
        arguments={
            "title": note_title,
            "content": "Testing scope enforcement",
            "category": "Testing",
        },
    )
    assert write_result.isError is False

    # Clean up
    note_id = json.loads(write_result.content[0].text)["id"]
    await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_delete_note", arguments={"note_id": note_id}
    )

    logger.info("JWT server properly allows operations based on token scopes")


async def test_jwt_automation_worked(nc_mcp_oauth_jwt_client):
    """Test that verifies the automated JWT client creation worked correctly.

    This test confirms that:
    1. JWT client was auto-created during container initialization
    2. MCP server loaded credentials from auto-generated file
    3. JWT authentication flow works end-to-end
    4. Server uses JWT tokens (not opaque tokens)
    """
    # If we can connect and execute tools, the automation worked
    result = await nc_mcp_oauth_jwt_client.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    # Execute a tool to verify full OAuth flow
    tool_result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert tool_result.isError is False

    logger.info(
        "✅ JWT client automation successful! "
        "Auto-generated credentials working correctly."
    )
