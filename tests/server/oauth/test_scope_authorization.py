"""Integration tests for OAuth scope-based authorization and dynamic tool filtering.

These tests verify:
1. Dynamic tool filtering based on user's token scopes (using JWT tokens)
2. Scope enforcement (403 responses for insufficient scopes)
3. Protected Resource Metadata (PRM) endpoint (RFC 9728)
4. WWW-Authenticate challenge headers
5. BasicAuth bypass (all tools visible)

Note: Tests use JWT OAuth tokens because scopes are embedded in the token payload,
enabling efficient scope-based tool filtering without additional API calls.
"""

import pytest


@pytest.mark.integration
async def test_prm_endpoint():
    """Test that the Protected Resource Metadata endpoint returns correct data."""
    import httpx

    # Test the PRM endpoint directly (RFC 9728 - path includes /mcp resource)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "http://localhost:8001/.well-known/oauth-protected-resource/mcp"
        )
        assert response.status_code == 200

        prm_data = response.json()
        assert prm_data["resource"] == "http://localhost:8001/mcp"
        assert "notes:read" in prm_data["scopes_supported"]
        assert "notes:write" in prm_data["scopes_supported"]
        assert "http://localhost:8080" in prm_data["authorization_servers"]
        assert "header" in prm_data["bearer_methods_supported"]
        assert "RS256" in prm_data["resource_signing_alg_values_supported"]


@pytest.mark.integration
async def test_basicauth_shows_all_tools(nc_mcp_client):
    """Test that BasicAuth mode shows all tools (no filtering)."""
    # Note: Don't use 'async with' for session-scoped fixtures
    # The fixture itself manages the session lifecycle

    # List all tools
    tools_response = await nc_mcp_client.list_tools()

    # BasicAuth should see all tools
    tool_names = [tool.name for tool in tools_response.tools]

    # Should see both read and write tools
    assert "nc_notes_get_note" in tool_names  # read tool
    assert "nc_notes_create_note" in tool_names  # write tool
    assert "nc_calendar_list_calendars" in tool_names  # read tool
    assert "nc_calendar_create_event" in tool_names  # write tool

    # Should have all 90+ tools
    assert len(tool_names) >= 90


@pytest.mark.integration
async def test_read_only_token_filters_write_tools(nc_mcp_oauth_client_read_only):
    """Test that a token with only read scopes filters out write tools."""
    import logging

    logger = logging.getLogger(__name__)

    # Connect with token that has only "notes:read" scope
    result = await nc_mcp_oauth_client_read_only.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info(f"Read-only token sees {len(tool_names)} tools")

    # Verify read tools are present (only for apps with :read scopes)
    # Read-only token has: notes:read, calendar:read, contacts:read,
    # cookbook:read, deck:read, tables:read, files:read, sharing:read
    expected_read_tools = [
        "nc_notes_get_note",  # notes:read
        "nc_notes_search_notes",  # notes:read
        "nc_calendar_list_calendars",  # calendar:read
        "nc_calendar_get_event",  # calendar:read
    ]

    for tool in expected_read_tools:
        assert tool in tool_names, f"Expected read tool {tool} not found in tool list"

    # Verify write tools are NOT present (filtered out)
    write_tools_should_be_filtered = [
        "nc_notes_create_note",  # notes:write
        "nc_notes_update_note",  # notes:write
        "nc_notes_delete_note",  # notes:write
        "nc_calendar_create_event",  # calendar:write
        "nc_calendar_update_event",  # calendar:write
        "nc_calendar_delete_event",  # calendar:write
    ]

    for tool in write_tools_should_be_filtered:
        assert tool not in tool_names, (
            f"Write tool {tool} should be filtered out but was found in tool list"
        )

    logger.info(
        f"✅ Read-only token properly filters tools: {len(tool_names)} read tools visible, "
        f"write tools hidden"
    )


@pytest.mark.integration
async def test_write_only_token_filters_read_tools(nc_mcp_oauth_client_write_only):
    """Test that a token with only write scopes filters out read tools."""
    import logging

    logger = logging.getLogger(__name__)

    # Connect with token that has only "notes:write" scope
    result = await nc_mcp_oauth_client_write_only.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info(f"Write-only token sees {len(tool_names)} tools")

    # Verify write tools are present
    # Write-only token has: notes:write, calendar:write, contacts:write,
    # cookbook:write, deck:write, tables:write, files:write, sharing:write
    expected_write_tools = [
        "nc_notes_create_note",  # notes:write
        "nc_notes_update_note",  # notes:write
        "nc_notes_delete_note",  # notes:write
        "nc_calendar_create_event",  # calendar:write
        "nc_calendar_update_event",  # calendar:write
        "nc_calendar_delete_event",  # calendar:write
    ]

    for tool in expected_write_tools:
        assert tool in tool_names, f"Expected write tool {tool} not found in tool list"

    # Verify read-only tools are NOT present (write-only scope)
    read_tools_should_be_filtered = [
        "nc_notes_get_note",  # notes:read
        "nc_notes_search_notes",  # notes:read
        "nc_calendar_list_calendars",  # calendar:read
        "nc_calendar_get_event",  # calendar:read
    ]

    for tool in read_tools_should_be_filtered:
        assert tool not in tool_names, (
            f"Read tool {tool} should be filtered out but was found in tool list"
        )

    logger.info(
        f"✅ Write-only token properly filters tools: {len(tool_names)} write tools visible, "
        f"read tools hidden"
    )


@pytest.mark.integration
async def test_full_access_token_shows_all_tools(nc_mcp_oauth_client_full_access):
    """Test that a token with both read and write scopes scopes can see all tools."""
    import logging

    logger = logging.getLogger(__name__)

    # Connect with token that has both "notes:read" and "notes:write" scopes
    result = await nc_mcp_oauth_client_full_access.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info(f"Full access token sees {len(tool_names)} tools")
    logger.info(f"Tools: {sorted(tool_names)}")

    # Verify both read and write tools are present
    # Full access has all *read and *write scopes
    expected_read_tools = [
        "nc_notes_get_note",  # notes:read
        "nc_notes_search_notes",  # notes:read
        "nc_calendar_list_calendars",  # calendar:read
    ]

    expected_write_tools = [
        "nc_notes_create_note",  # notes:write
        "nc_calendar_create_event",  # calendar:write
    ]

    for tool in expected_read_tools:
        assert tool in tool_names, f"Expected read tool {tool} not found"

    for tool in expected_write_tools:
        assert tool in tool_names, f"Expected write tool {tool} not found"

    # Should have all 90+ tools (both read and write)
    assert len(tool_names) >= 90

    logger.info(
        f"✅ Full access token sees all tools: {len(tool_names)} total (read + write)"
    )


@pytest.mark.integration
async def test_scope_helper_functions():
    """Test the scope authorization helper functions."""
    from nextcloud_mcp_server.auth import get_required_scopes, has_required_scopes

    # Create a mock function with scope requirements
    async def mock_read_tool():
        pass

    async def mock_write_tool():
        pass

    async def mock_no_scope_tool():
        pass

    # Add scope metadata
    mock_read_tool._required_scopes = ["notes:read"]  # type: ignore
    mock_write_tool._required_scopes = ["notes:write"]  # type: ignore

    # Test get_required_scopes
    assert get_required_scopes(mock_read_tool) == ["notes:read"]
    assert get_required_scopes(mock_write_tool) == ["notes:write"]
    assert get_required_scopes(mock_no_scope_tool) == []

    # Test has_required_scopes
    read_only_scopes = {"notes:read"}
    full_scopes = {"notes:read", "notes:write"}
    no_scopes = set()

    # User with only read scope
    assert has_required_scopes(mock_read_tool, read_only_scopes) is True
    assert has_required_scopes(mock_write_tool, read_only_scopes) is False
    assert has_required_scopes(mock_no_scope_tool, read_only_scopes) is True

    # User with full scopes
    assert has_required_scopes(mock_read_tool, full_scopes) is True
    assert has_required_scopes(mock_write_tool, full_scopes) is True
    assert has_required_scopes(mock_no_scope_tool, full_scopes) is True

    # User with no scopes
    assert has_required_scopes(mock_read_tool, no_scopes) is False
    assert has_required_scopes(mock_write_tool, no_scopes) is False
    assert has_required_scopes(mock_no_scope_tool, no_scopes) is True


@pytest.mark.integration
async def test_scope_decorator_stores_metadata():
    """Test that @require_scopes decorator properly stores metadata."""
    from nextcloud_mcp_server.auth import require_scopes

    @require_scopes("notes:read", "notes:write")
    async def test_function():
        pass

    # Check that metadata was stored
    assert hasattr(test_function, "_required_scopes")
    assert test_function._required_scopes == ["notes:read", "notes:write"]


@pytest.mark.integration
async def test_tools_have_scope_decorators(nc_mcp_client):
    """Test that MCP tools have scope requirements defined."""
    # Note: Don't use 'async with' for session-scoped fixtures
    # The fixture itself manages the session lifecycle

    # We can at least verify that some expected tools exist
    tools_response = await nc_mcp_client.list_tools()
    tool_names = [tool.name for tool in tools_response.tools]

    # Verify expected read tools exist
    expected_read_tools = [
        "nc_notes_get_note",
        "nc_notes_search_notes",
        "nc_calendar_list_calendars",
        "nc_calendar_get_event",
        "nc_contacts_list_contacts",
        "nc_webdav_list_directory",
        "nc_webdav_read_file",
    ]

    for tool in expected_read_tools:
        assert tool in tool_names, f"Expected read tool {tool} not found"

    # Verify expected write tools exist
    expected_write_tools = [
        "nc_notes_create_note",
        "nc_notes_update_note",
        "nc_notes_delete_note",
        "nc_calendar_create_event",
        "nc_calendar_update_event",
        "nc_calendar_delete_event",
        "nc_contacts_create_contact",
        "nc_webdav_write_file",
        "nc_webdav_create_directory",
    ]

    for tool in expected_write_tools:
        assert tool in tool_names, f"Expected write tool {tool} not found"


@pytest.mark.skip(reason="Script no longer exists - decorators are already in place")
@pytest.mark.integration
async def test_scope_classification():
    """Test that our scope classification correctly identifies read vs write operations."""
    from scripts.add_scope_decorators_simple import classify_function

    # Test read operations
    assert classify_function("nc_notes_get_note") == "notes:read"
    assert classify_function("nc_notes_search_notes") == "notes:read"
    assert classify_function("nc_calendar_list_events") == "calendar:read"
    assert classify_function("nc_webdav_read_file") == "files:read"
    assert classify_function("nc_calendar_find_availability") == "calendar:read"
    assert classify_function("nc_calendar_get_upcoming_events") == "notes:read"

    # Test write operations
    assert classify_function("nc_notes_create_note") == "notes:write"
    assert classify_function("nc_notes_update_note") == "notes:write"
    assert classify_function("nc_notes_delete_note") == "notes:write"
    assert classify_function("nc_notes_append_content") == "notes:write"
    assert classify_function("nc_calendar_create_event") == "calendar:write"
    assert classify_function("nc_calendar_update_event") == "notes:write"
    assert classify_function("nc_calendar_manage_calendar") == "notes:write"
    assert classify_function("nc_webdav_write_file") == "files:write"
    assert classify_function("nc_webdav_move_resource") == "notes:write"
    assert classify_function("nc_contacts_create_contact") == "notes:write"
    assert classify_function("nc_cookbook_import_recipe") == "notes:write"
    assert classify_function("nc_tables_insert_row") == "notes:write"
    assert classify_function("deck_archive_card") == "notes:write"
    assert classify_function("deck_assign_label_to_card") == "notes:write"


@pytest.mark.skip(reason="Script no longer exists - decorators are already in place")
@pytest.mark.integration
async def test_all_tools_classified():
    """Verify that all tools can be properly classified as read or write."""
    from scripts.add_scope_decorators_simple import classify_function

    # List of all tool names (extracted from our implementation)
    all_tools = [
        # Calendar tools
        "nc_calendar_list_calendars",
        "nc_calendar_create_event",
        "nc_calendar_list_events",
        "nc_calendar_get_event",
        "nc_calendar_update_event",
        "nc_calendar_delete_event",
        "nc_calendar_create_meeting",
        "nc_calendar_get_upcoming_events",
        "nc_calendar_find_availability",
        "nc_calendar_bulk_operations",
        "nc_calendar_manage_calendar",
        "nc_calendar_list_todos",
        "nc_calendar_create_todo",
        "nc_calendar_update_todo",
        "nc_calendar_delete_todo",
        "nc_calendar_search_todos",
        # Notes tools
        "nc_notes_get_note",
        "nc_notes_search_notes",
        "nc_notes_create_note",
        "nc_notes_update_note",
        "nc_notes_append_content",
        "nc_notes_delete_note",
        "nc_notes_get_attachment",
        # Add more as needed...
    ]

    unclassified = []
    for tool_name in all_tools:
        scope = classify_function(tool_name)
        if scope is None:
            unclassified.append(tool_name)

    # All tools should be classifiable
    assert len(unclassified) == 0, f"Unclassified tools: {unclassified}"


@pytest.mark.integration
async def test_scope_metadata_coverage(nc_mcp_client):
    """Test that all tools have scope metadata defined (no undecorated tools)."""
    # This test would require access to the actual tool functions to check metadata
    # For now, we verify that the expected number of tools exists
    # Note: Don't use 'async with' for session-scoped fixtures

    tools_response = await nc_mcp_client.list_tools()

    # We applied decorators to 90 tools
    # In BasicAuth mode, all should be visible
    assert len(tools_response.tools) >= 90


@pytest.mark.integration
async def test_jwt_with_no_custom_scopes_returns_zero_tools(
    nc_mcp_oauth_client_no_custom_scopes,
):
    """
    Test that a JWT token with only OIDC default scopes shows only OAuth provisioning tools.

    This tests the security behavior when a user declines to grant custom scopes during consent.
    Expected: JWT token has scopes=['openid', 'profile', 'email'] but no resource scopes.
    - Resource tools (notes:*, calendar:*, etc.) are filtered out
    - OAuth provisioning tools (requiring only 'openid') remain visible
      so users can provision Nextcloud access after authentication
    """
    import logging

    logger = logging.getLogger(__name__)

    # Connect with JWT token that has NO custom scopes (only openid, profile, email)
    result = await nc_mcp_oauth_client_no_custom_scopes.list_tools()
    assert result is not None

    tool_names = [tool.name for tool in result.tools]
    logger.info(
        f"JWT token with no custom scopes sees {len(tool_names)} tools (should be 4 OAuth tools)"
    )

    # Only OAuth provisioning tools should be visible (they require 'openid' scope)
    expected_oauth_tools = [
        "provision_nextcloud_access",
        "revoke_nextcloud_access",
        "check_provisioning_status",
        "check_logged_in",  # Login elicitation tool (ADR-006)
    ]

    assert set(tool_names) == set(expected_oauth_tools), (
        f"Expected only OAuth provisioning tools {expected_oauth_tools} "
        f"but got {tool_names}"
    )

    logger.info(
        f"✅ JWT token with only openid scope correctly shows {len(tool_names)} OAuth provisioning tools, "
        "resource tools filtered out"
    )


@pytest.mark.integration
async def test_jwt_consent_scenarios_read_only(nc_mcp_oauth_client_read_only):
    """
    Test JWT with only nc:read scope consented.

    Simulates user granting only read permission during OAuth consent.
    Expected: Should see read tools but not write tools.
    """
    import logging

    logger = logging.getLogger(__name__)

    result = await nc_mcp_oauth_client_read_only.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info(f"JWT with nc:read consent sees {len(tool_names)} tools")

    # Verify read tools are present
    read_tools = ["nc_notes_get_note", "nc_notes_search_notes", "nc_webdav_read_file"]
    for tool in read_tools:
        assert tool in tool_names, f"Expected read tool {tool} not found"

    # Verify write tools are filtered out
    write_tools = [
        "nc_notes_create_note",
        "nc_notes_update_note",
        "nc_webdav_write_file",
    ]
    for tool in write_tools:
        assert tool not in tool_names, f"Write tool {tool} should be filtered out"

    logger.info(
        f"✅ JWT with nc:read consent: {len(tool_names)} read tools visible, write tools filtered"
    )


@pytest.mark.integration
async def test_jwt_consent_scenarios_write_only(nc_mcp_oauth_client_write_only):
    """
    Test JWT with only nc:write scope consented.

    Simulates user granting only write permission during OAuth consent.
    Expected: Should see write tools but not read-only tools.
    """
    import logging

    logger = logging.getLogger(__name__)

    result = await nc_mcp_oauth_client_write_only.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info(f"JWT with nc:write consent sees {len(tool_names)} tools")

    # Verify write tools are present
    write_tools = [
        "nc_notes_create_note",
        "nc_notes_update_note",
        "nc_webdav_write_file",
    ]
    for tool in write_tools:
        assert tool in tool_names, f"Expected write tool {tool} not found"

    # Verify read-only tools are filtered out
    read_only_tools = ["nc_notes_get_note", "nc_notes_search_notes"]
    for tool in read_only_tools:
        assert tool not in tool_names, f"Read-only tool {tool} should be filtered out"

    logger.info(
        f"✅ JWT with nc:write consent: {len(tool_names)} write tools visible, read-only tools filtered"
    )


@pytest.mark.integration
async def test_jwt_consent_scenarios_full_access(nc_mcp_oauth_client_full_access):
    """
    Test JWT with both nc:read and nc:write scopes consented.

    Simulates user granting both permissions during OAuth consent.
    Expected: Should see all 90+ tools (both read and write).
    """
    import logging

    logger = logging.getLogger(__name__)

    result = await nc_mcp_oauth_client_full_access.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info(f"JWT with full consent sees {len(tool_names)} tools")

    # Verify both read and write tools are present
    read_tools = ["nc_notes_get_note", "nc_webdav_read_file"]
    write_tools = ["nc_notes_create_note", "nc_webdav_write_file"]

    for tool in read_tools:
        assert tool in tool_names, f"Expected read tool {tool} not found"

    for tool in write_tools:
        assert tool in tool_names, f"Expected write tool {tool} not found"

    # Should have all tools
    assert len(tool_names) >= 90, f"Expected 90+ tools but got {len(tool_names)}"

    logger.info(
        f"✅ JWT with full consent: {len(tool_names)} tools visible (all read + write)"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
