"""Integration tests for multi-user BasicAuth pass-through mode.

Tests that BasicAuth credentials are extracted from request headers
and passed through to Nextcloud APIs without storage (stateless).
"""

import json

import pytest


@pytest.mark.integration
async def test_basic_auth_pass_through_notes_search(nc_mcp_basic_auth_client):
    """Test BasicAuth pass-through with notes search tool."""
    # Call tool - BasicAuth header is set at connection level by fixture
    response = await nc_mcp_basic_auth_client.call_tool(
        "nc_notes_search_notes", {"query": "test"}
    )

    # Verify tool executed successfully with pass-through auth
    assert response is not None
    assert not response.isError, f"Tool returned error: {response.content}"
    # Response should have content with results
    assert len(response.content) > 0
    data = json.loads(response.content[0].text)
    assert "results" in data


@pytest.mark.integration
async def test_basic_auth_pass_through_notes_create(nc_mcp_basic_auth_client):
    """Test BasicAuth pass-through with notes create tool."""
    # Create a note using BasicAuth
    response = await nc_mcp_basic_auth_client.call_tool(
        "nc_notes_create_note",
        {
            "title": "BasicAuth Test Note",
            "content": "This note was created via BasicAuth pass-through",
            "category": "Test",
        },
    )

    assert response is not None
    assert not response.isError, f"Tool returned error: {response.content}"
    # Parse response and verify note was created
    data = json.loads(response.content[0].text)
    assert data.get("success") is True or "note_id" in data


@pytest.mark.integration
async def test_basic_auth_pass_through_get_note(nc_mcp_basic_auth_client):
    """Test BasicAuth pass-through with get note tool."""
    # First create a note to get
    create_response = await nc_mcp_basic_auth_client.call_tool(
        "nc_notes_create_note",
        {
            "title": "BasicAuth Get Test",
            "content": "Note to retrieve",
            "category": "Test",
        },
    )
    assert not create_response.isError
    create_data = json.loads(create_response.content[0].text)
    note_id = create_data.get("id")

    # Now get the note using BasicAuth
    response = await nc_mcp_basic_auth_client.call_tool(
        "nc_notes_get_note", {"note_id": note_id}
    )

    assert response is not None
    assert not response.isError, f"Tool returned error: {response.content}"
    data = json.loads(response.content[0].text)
    # Nextcloud may append a number to duplicate titles
    assert data.get("title", "").startswith("BasicAuth Get Test")
