"""Integration tests for OAuth scope-based authorization and dynamic tool filtering.

These tests verify:
1. Dynamic tool filtering based on user's token scopes (using JWT tokens)
2. Scope enforcement on the tools/call path (stored app-password scopes)
3. Protected Resource Metadata (PRM) endpoint (RFC 9728)
4. BasicAuth bypass (all tools visible)

Note: Tests use JWT OAuth tokens because scopes are embedded in the token payload,
enabling efficient scope-based tool filtering without additional API calls.

FILTERING VS ENFORCEMENT — these are separate layers and both need coverage.
``list_tools`` filtering is advisory: it changes what a well-behaved client is
offered, but a client can still invoke a tool it was never shown. Only
``@require_scopes`` on the tools/call path actually denies. For a long time
every test in this file asserted filtering alone and none called a tool, which
is why a regression that made ``@require_scopes`` a runtime no-op went
unnoticed. ``test_stored_scopes_enforced_on_tool_call`` closes that gap here;
tests/unit/test_require_scopes_enforcement.py covers the decorator directly.

Enforcement in login-flow mode has two layers and both must hold on the call
path. Nextcloud app passwords are unscoped — all-or-nothing against the
Nextcloud API — so this server applies its own limits:

    token(consent ∩ client allowed_scopes) ∩ stored (NULL = no restriction)

The stored per-user scope set can only narrow; a NULL grant narrows nothing and
leaves the OAuth token as the ceiling. It used to short-circuit the check
entirely, which let a NULL-scoped session call tools its token never carried.

Not covered here: WWW-Authenticate challenge headers for step-up auth.
"""

import json
import logging
import os

import httpx
import pytest
from mcp.types import CallToolResult

from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES
from tests.server.login_flow.conftest import LOGIN_FLOW_MCP_BASE_URL

logger = logging.getLogger(__name__)


@pytest.mark.integration
@pytest.mark.login_flow
async def test_prm_endpoint():
    """Test that the Protected Resource Metadata endpoint returns correct data."""

    # Test the PRM endpoint directly (RFC 9728 - path includes /mcp resource)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "http://localhost:8004/.well-known/oauth-protected-resource/mcp"
        )
        assert response.status_code == 200

        prm_data = response.json()
        assert prm_data["resource"] == "http://localhost:8004/mcp"
        assert "notes.read" in prm_data["scopes_supported"]
        assert "notes.write" in prm_data["scopes_supported"]
        assert "http://localhost:8004" in prm_data["authorization_servers"]
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
@pytest.mark.login_flow
async def test_read_only_token_filters_write_tools(nc_mcp_login_flow_client_read_only):
    """Test that a token with only read scopes filters out write tools."""

    # Connect with token that has only "notes.read" scope
    result = await nc_mcp_login_flow_client_read_only.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info("Read-only token sees %s tools", len(tool_names))

    # Verify read tools are present (only for apps with :read scopes)
    # Read-only token has: notes.read, calendar.read, contacts.read,
    # cookbook.read, deck.read, tables.read, files.read, sharing.read
    expected_read_tools = [
        "nc_notes_get_note",  # notes.read
        "nc_notes_search_notes",  # notes.read
        "nc_calendar_list_calendars",  # calendar.read
        "nc_calendar_get_event",  # calendar.read
    ]

    for tool in expected_read_tools:
        assert tool in tool_names, f"Expected read tool {tool} not found in tool list"

    # Verify write tools are NOT present (filtered out)
    write_tools_should_be_filtered = [
        "nc_notes_create_note",  # notes.write
        "nc_notes_update_note",  # notes.write
        "nc_notes_delete_note",  # notes.write
        "nc_calendar_create_event",  # calendar.write
        "nc_calendar_update_event",  # calendar.write
        "nc_calendar_delete_event",  # calendar.write
    ]

    for tool in write_tools_should_be_filtered:
        assert tool not in tool_names, (
            f"Write tool {tool} should be filtered out but was found in tool list"
        )

    logger.info(
        "✅ Read-only token properly filters tools: %s read tools visible, write tools hidden",
        len(tool_names),
    )


async def _set_stored_scopes(user_id: str, password: str, scopes: list[str]) -> None:
    """Rewrite the caller's stored app-password scopes via the management API.

    PATCH /api/v1/users/{user_id}/scopes changes only the stored scope set —
    the app password itself is untouched and stays valid — and invalidates the
    server's scope cache, so the next tool call sees the new set immediately.
    That makes it the cheapest way to put a provisioned session into a
    restricted state without driving a second browser login flow.
    """
    async with httpx.AsyncClient() as client:
        response = await client.patch(
            f"{LOGIN_FLOW_MCP_BASE_URL}/api/v1/users/{user_id}/scopes",
            json={"scopes": scopes},
            auth=(user_id, password),
            timeout=30.0,
        )

    assert response.status_code == 200, (
        f"Could not set stored scopes for {user_id!r} "
        f"(HTTP {response.status_code}): {response.text}. "
        "A 403 'User ID mismatch' means the OIDC sub differs from the Nextcloud "
        "UID, so the management API and @require_scopes key the app-password "
        "record differently — that is a real bug, not a test-setup problem."
    )


def _tool_text(result: CallToolResult) -> str:
    """Flatten a tool result's content blocks into one searchable string."""
    return " ".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


@pytest.mark.integration
@pytest.mark.login_flow
async def test_semantic_tool_callable_after_default_provisioning(
    nc_mcp_login_flow_client,
):
    """A semantic tool must be callable by a default-provisioned user (GH #1277).

    ``semantic.read`` was required by the semantic tools but absent from
    ALL_SUPPORTED_SCOPES, so the provisioning tool — whose default was that very
    set — could never grant it, and nc_auth_update_scopes rejected it as
    invalid. Every semantic call was denied with no supported way to recover.

    nc_get_vector_sync_status is used rather than nc_semantic_search because it
    needs no indexed content: the assertion here is about authorization, not
    retrieval.
    """
    result = await nc_mcp_login_flow_client.call_tool("nc_get_vector_sync_status", {})

    assert not result.is_error, (
        "nc_get_vector_sync_status was denied for a default-provisioned user: "
        f"{_tool_text(result)}"
    )


@pytest.mark.integration
@pytest.mark.login_flow
async def test_token_scopes_enforced_on_tool_call(
    nc_mcp_login_flow_client,
    nc_mcp_login_flow_client_read_only,
):
    """A tool call must be DENIED when the *token* doesn't carry the scope.

    The stored app-password grant is an additional restriction, not a
    replacement for the token: default provisioning stores NULL ("no additional
    restriction"), and the OAuth token remains the ceiling. Before that, NULL
    short-circuited the whole check, so a read-only token could create notes —
    and list_tools (filtered on token scopes) disagreed with tools/call.

    Depends on nc_mcp_login_flow_client only to guarantee the user is
    provisioned; the read-only session is what makes the call. The assertion
    holds whether the stored grant is NULL or an explicit list, since both
    layers must permit the scope.
    """
    result = await nc_mcp_login_flow_client_read_only.call_tool(
        "nc_notes_create_note",
        {
            "title": "token-scope-enforcement-probe",
            "content": "must never be created",
            "category": "testing",
        },
    )

    assert result.is_error, (
        "nc_notes_create_note succeeded with a read-only token — the token "
        "layer is not enforcing on the tools/call path"
    )
    message = _tool_text(result)
    assert "notes.write" in message, (
        f"expected the missing scope to be named in the denial, got: {message}"
    )


@pytest.mark.integration
@pytest.mark.login_flow
async def test_stored_scopes_enforced_on_tool_call(nc_mcp_login_flow_client):
    """A tool call must be DENIED when the stored scopes don't cover it.

    This is the property that actually protects data. Nextcloud app passwords
    carry no scopes of their own, so once a session holds one it can reach the
    whole Nextcloud API; the per-user scope set this server stores alongside
    the password is the only limit, and it is enforced application-side by
    ``@require_scopes`` on the tools/call path.

    Restricting the stored scopes to notes.read and then calling a write tool
    exercises exactly that. Asserting the read tool still succeeds afterwards
    keeps the test honest: it proves the write was refused *for lack of scope*,
    not because the session broke.
    """
    session = nc_mcp_login_flow_client

    status_result = await session.call_tool("nc_auth_check_status", {})
    status = json.loads(status_result.content[0].text)
    assert status.get("status") == "provisioned", (
        f"fixture session is not provisioned: {status}"
    )

    user_id = status["user_id"]
    original_scopes = status.get("scopes")
    password = os.getenv("NEXTCLOUD_PASSWORD")
    assert password, "NEXTCLOUD_PASSWORD env var not set"

    try:
        await _set_stored_scopes(user_id, password, ["notes.read"])

        write_result = await session.call_tool(
            "nc_notes_create_note",
            {
                "title": "scope-enforcement-probe",
                "content": "must never be created",
                "category": "testing",
            },
        )
        assert write_result.is_error, (
            "nc_notes_create_note succeeded with stored scopes ['notes.read'] — "
            "@require_scopes is not enforcing on the tools/call path"
        )
        message = _tool_text(write_result)
        assert "notes.write" in message, (
            f"expected the missing scope to be named in the denial, got: {message}"
        )

        read_result = await session.call_tool(
            "nc_notes_search_notes", {"query": "scope-enforcement-probe"}
        )
        assert not read_result.is_error, (
            "notes.read was granted but the read tool was refused: "
            f"{_tool_text(read_result)}"
        )
    finally:
        # Session-scoped fixture: other tests reuse this provisioning, so put
        # the scope set back. Fall back to the full set rather than [], which
        # would leave every later test denied. Never raise from here — an
        # exception in `finally` would replace whatever the test was actually
        # failing on.
        #
        # The fallback cannot restore NULL (the scopes endpoint takes a list),
        # but it is equivalent for later tests: the full set covers every scope
        # a token can carry, so `stored ∧ token` reduces to the token either
        # way — which is exactly what NULL means.
        try:
            await _set_stored_scopes(
                user_id,
                password,
                original_scopes
                if isinstance(original_scopes, list) and original_scopes
                else sorted(ALL_SUPPORTED_SCOPES),
            )
        except Exception:
            logger.exception(
                "Failed to restore stored scopes for %s — later login_flow "
                "tests in this session may see a restricted scope set",
                user_id,
            )


@pytest.mark.integration
@pytest.mark.login_flow
async def test_write_only_token_filters_read_tools(nc_mcp_login_flow_client_write_only):
    """Test that a token with only write scopes filters out read tools."""

    # Connect with token that has only "notes.write" scope
    result = await nc_mcp_login_flow_client_write_only.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info("Write-only token sees %s tools", len(tool_names))

    # Verify write tools are present
    # Write-only token has: notes.write, calendar.write, contacts.write,
    # cookbook.write, deck.write, tables.write, files.write, sharing.write
    expected_write_tools = [
        "nc_notes_create_note",  # notes.write
        "nc_notes_update_note",  # notes.write
        "nc_notes_delete_note",  # notes.write
        "nc_calendar_create_event",  # calendar.write
        "nc_calendar_update_event",  # calendar.write
        "nc_calendar_delete_event",  # calendar.write
    ]

    for tool in expected_write_tools:
        assert tool in tool_names, f"Expected write tool {tool} not found in tool list"

    # Verify read-only tools are NOT present (write-only scope)
    read_tools_should_be_filtered = [
        "nc_notes_get_note",  # notes.read
        "nc_notes_search_notes",  # notes.read
        "nc_calendar_list_calendars",  # calendar.read
        "nc_calendar_get_event",  # calendar.read
    ]

    for tool in read_tools_should_be_filtered:
        assert tool not in tool_names, (
            f"Read tool {tool} should be filtered out but was found in tool list"
        )

    logger.info(
        "✅ Write-only token properly filters tools: %s write tools visible, read tools hidden",
        len(tool_names),
    )


@pytest.mark.integration
@pytest.mark.login_flow
async def test_full_access_token_shows_all_tools(nc_mcp_login_flow_client_full_access):
    """Test that a token with both read and write scopes scopes can see all tools."""

    # Connect with token that has both "notes.read" and "notes.write" scopes
    result = await nc_mcp_login_flow_client_full_access.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info("Full access token sees %s tools", len(tool_names))
    logger.info("Tools: %s", sorted(tool_names))

    # Verify both read and write tools are present
    # Full access has all *read and *write scopes
    expected_read_tools = [
        "nc_notes_get_note",  # notes.read
        "nc_notes_search_notes",  # notes.read
        "nc_calendar_list_calendars",  # calendar.read
    ]

    expected_write_tools = [
        "nc_notes_create_note",  # notes.write
        "nc_calendar_create_event",  # calendar.write
    ]

    for tool in expected_read_tools:
        assert tool in tool_names, f"Expected read tool {tool} not found"

    for tool in expected_write_tools:
        assert tool in tool_names, f"Expected write tool {tool} not found"

    # Should have all 90+ tools (both read and write)
    assert len(tool_names) >= 90

    logger.info(
        "✅ Full access token sees all tools: %s total (read + write)", len(tool_names)
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
    mock_read_tool._required_scopes = ["notes.read"]  # type: ignore
    mock_write_tool._required_scopes = ["notes.write"]  # type: ignore

    # Test get_required_scopes
    assert get_required_scopes(mock_read_tool) == ["notes.read"]
    assert get_required_scopes(mock_write_tool) == ["notes.write"]
    assert get_required_scopes(mock_no_scope_tool) == []

    # Test has_required_scopes
    read_only_scopes = {"notes.read"}
    full_scopes = {"notes.read", "notes.write"}
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

    @require_scopes("notes.read", "notes.write")
    async def test_function():
        pass

    # Check that metadata was stored
    assert hasattr(test_function, "_required_scopes")
    assert test_function._required_scopes == ["notes.read", "notes.write"]


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
    # `scripts/` is a dev-only helper dir (not an installed package); resolved
    # at runtime via the repo root on sys.path, so ty can't see it.
    from scripts.add_scope_decorators_simple import (  # ty: ignore[unresolved-import]
        classify_function,
    )

    # Test read operations
    assert classify_function("nc_notes_get_note") == "notes.read"
    assert classify_function("nc_notes_search_notes") == "notes.read"
    assert classify_function("nc_calendar_list_events") == "calendar.read"
    assert classify_function("nc_webdav_read_file") == "files.read"
    assert classify_function("nc_calendar_find_availability") == "calendar.read"
    assert classify_function("nc_calendar_get_upcoming_events") == "notes.read"

    # Test write operations
    assert classify_function("nc_notes_create_note") == "notes.write"
    assert classify_function("nc_notes_update_note") == "notes.write"
    assert classify_function("nc_notes_delete_note") == "notes.write"
    assert classify_function("nc_notes_append_content") == "notes.write"
    assert classify_function("nc_calendar_create_event") == "calendar.write"
    assert classify_function("nc_calendar_update_event") == "notes.write"
    assert classify_function("nc_calendar_manage_calendar") == "notes.write"
    assert classify_function("nc_webdav_write_file") == "files.write"
    assert classify_function("nc_webdav_move_resource") == "notes.write"
    assert classify_function("nc_contacts_create_contact") == "notes.write"
    assert classify_function("nc_cookbook_import_recipe") == "notes.write"
    assert classify_function("nc_tables_insert_row") == "notes.write"
    assert classify_function("deck_archive_card") == "notes.write"
    assert classify_function("deck_assign_label_to_card") == "notes.write"


@pytest.mark.skip(reason="Script no longer exists - decorators are already in place")
@pytest.mark.integration
async def test_all_tools_classified():
    """Verify that all tools can be properly classified as read or write."""
    # `scripts/` is a dev-only helper dir (not an installed package); resolved
    # at runtime via the repo root on sys.path, so ty can't see it.
    from scripts.add_scope_decorators_simple import (  # ty: ignore[unresolved-import]
        classify_function,
    )

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
@pytest.mark.login_flow
async def test_jwt_with_no_custom_scopes_returns_zero_tools(
    nc_mcp_login_flow_client_no_custom_scopes,
):
    """
    Test that a JWT token with only OIDC default scopes shows only OAuth provisioning tools.

    This tests the security behavior when a user declines to grant custom scopes during consent.
    Expected: JWT token has scopes=['openid', 'profile', 'email'] but no resource scopes.
    - Resource tools (notes:*, calendar:*, etc.) are filtered out
    - OAuth provisioning tools (requiring only 'openid') remain visible
      so users can provision Nextcloud access after authentication
    """

    # Connect with JWT token that has NO custom scopes (only openid, profile, email)
    result = await nc_mcp_login_flow_client_no_custom_scopes.list_tools()
    assert result is not None

    tool_names = [tool.name for tool in result.tools]
    logger.info(
        "JWT token with no custom scopes sees %s tools (should be 7 auth tools)",
        len(tool_names),
    )

    # Only auth/provisioning tools should be visible (they require 'openid' scope)
    expected_auth_tools = [
        "provision_nextcloud_access",
        "revoke_nextcloud_access",
        "check_provisioning_status",
        "check_logged_in",  # Login elicitation tool (ADR-006)
        "nc_auth_provision_access",  # Login Flow v2 (ADR-022)
        "nc_auth_check_status",  # Login Flow v2
        "nc_auth_update_scopes",  # Login Flow v2
    ]

    assert set(tool_names) == set(expected_auth_tools), (
        f"Expected only auth/provisioning tools {expected_auth_tools} "
        f"but got {tool_names}"
    )

    logger.info(
        "✅ JWT token with only openid scope correctly shows %s auth tools, resource tools filtered out",
        len(tool_names),
    )


@pytest.mark.integration
@pytest.mark.login_flow
async def test_jwt_consent_scenarios_read_only(nc_mcp_login_flow_client_read_only):
    """
    Test JWT with only nc:read scope consented.

    Simulates user granting only read permission during OAuth consent.
    Expected: Should see read tools but not write tools.
    """

    result = await nc_mcp_login_flow_client_read_only.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info("JWT with nc:read consent sees %s tools", len(tool_names))

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
        "✅ JWT with nc:read consent: %s read tools visible, write tools filtered",
        len(tool_names),
    )


@pytest.mark.integration
@pytest.mark.login_flow
async def test_jwt_consent_scenarios_write_only(nc_mcp_login_flow_client_write_only):
    """
    Test JWT with only nc:write scope consented.

    Simulates user granting only write permission during OAuth consent.
    Expected: Should see write tools but not read-only tools.
    """

    result = await nc_mcp_login_flow_client_write_only.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info("JWT with nc:write consent sees %s tools", len(tool_names))

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
        "✅ JWT with nc:write consent: %s write tools visible, read-only tools filtered",
        len(tool_names),
    )


@pytest.mark.integration
@pytest.mark.login_flow
async def test_jwt_consent_scenarios_full_access(nc_mcp_login_flow_client_full_access):
    """
    Test JWT with both nc:read and nc:write scopes consented.

    Simulates user granting both permissions during OAuth consent.
    Expected: Should see all 90+ tools (both read and write).
    """

    result = await nc_mcp_login_flow_client_full_access.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    tool_names = [tool.name for tool in result.tools]
    logger.info("JWT with full consent sees %s tools", len(tool_names))

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
        "✅ JWT with full consent: %s tools visible (all read + write)", len(tool_names)
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
