"""Integration tests for multi-user BasicAuth pass-through mode.

Tests that BasicAuth credentials are extracted from request headers
and passed through to Nextcloud APIs without storage (stateless).
"""

import pytest


@pytest.mark.integration
async def test_basic_auth_pass_through_notes_list(nc_mcp_basic_auth_client):
    """Test BasicAuth pass-through with notes list tool."""
    # Call tool - BasicAuth header is set at connection level by fixture
    response = await nc_mcp_basic_auth_client.call_tool("nc_notes_list", {})

    # Verify tool executed successfully with pass-through auth
    assert response is not None
    assert "results" in response or "content" in response


@pytest.mark.integration
async def test_basic_auth_pass_through_notes_create(nc_mcp_basic_auth_client):
    """Test BasicAuth pass-through with notes create tool."""
    # Create a note using BasicAuth
    response = await nc_mcp_basic_auth_client.call_tool(
        "nc_notes_create",
        {
            "title": "BasicAuth Test Note",
            "content": "This note was created via BasicAuth pass-through",
            "category": "Test",
        },
    )

    assert response is not None
    assert response.get("success") is True or "note_id" in response


@pytest.mark.integration
async def test_basic_auth_pass_through_search(nc_mcp_basic_auth_client):
    """Test BasicAuth pass-through with search tool."""
    # Search notes using BasicAuth
    response = await nc_mcp_basic_auth_client.call_tool(
        "nc_notes_search", {"query": "BasicAuth"}
    )

    assert response is not None
    assert "results" in response or "content" in response
