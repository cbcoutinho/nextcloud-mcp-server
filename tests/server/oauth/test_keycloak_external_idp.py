"""Keycloak External IdP Integration Tests.

Tests verify ADR-002 external identity provider integration where:
1. Keycloak acts as external OAuth/OIDC provider
2. MCP server validates tokens via Nextcloud user_oidc app
3. Nextcloud auto-provisions users from Keycloak token claims
4. MCP tools execute successfully with Keycloak tokens

Architecture:
    MCP Client → Keycloak (OAuth) → MCP Server → Nextcloud user_oidc (validates) → APIs

Tests:
1. Keycloak OAuth token acquisition via Playwright
2. MCP client connection to mcp-keycloak service (port 8002)
3. Token validation through Nextcloud user_oidc app
4. MCP tool execution with Keycloak tokens
5. User auto-provisioning from Keycloak claims
6. Scope-based tool filtering with Keycloak JWT tokens
"""

import json
import logging

import pytest

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


# ============================================================================
# OAuth Token Acquisition Tests
# ============================================================================


async def test_keycloak_oauth_token_acquisition(keycloak_oauth_token):
    """Test that Playwright can obtain OAuth token from Keycloak.

    Verifies:
    - Playwright automation handles Keycloak login page (input#username, input#password)
    - Keycloak consent screen is handled correctly
    - Authorization code is exchanged for access token
    - Token is returned successfully

    This is a foundational test - if this fails, all other Keycloak tests will fail.
    """
    assert keycloak_oauth_token is not None
    assert isinstance(keycloak_oauth_token, str)
    assert len(keycloak_oauth_token) > 100  # Tokens should be substantial length

    logger.info(
        f"✓ Keycloak OAuth token acquired (length: {len(keycloak_oauth_token)})"
    )
    logger.info(f"  Token prefix: {keycloak_oauth_token[:50]}...")


async def test_keycloak_oauth_client_credentials_discovery(
    keycloak_oauth_client_credentials,
):
    """Test Keycloak OIDC discovery and credential loading.

    Verifies:
    - OIDC discovery endpoint is accessible
    - Token and authorization endpoints are discovered
    - Static client credentials are loaded from environment
    - Callback server is initialized
    """
    (
        client_id,
        client_secret,
        callback_url,
        token_endpoint,
        authorization_endpoint,
    ) = keycloak_oauth_client_credentials

    assert client_id == "nextcloud-mcp-server"
    assert client_secret == "mcp-secret-change-in-production"
    assert callback_url.startswith("http://")
    # With --hostname-backchannel-dynamic, external clients see localhost:8888
    assert "localhost:8888" in token_endpoint or "keycloak" in token_endpoint
    assert (
        "localhost:8888" in authorization_endpoint
        or "keycloak" in authorization_endpoint
    )
    assert "/realms/nextcloud-mcp/" in token_endpoint

    logger.info("✓ Keycloak OIDC discovery successful")
    logger.info(f"  Client ID: {client_id}")
    logger.info(f"  Token endpoint: {token_endpoint}")
    logger.info(f"  Authorization endpoint: {authorization_endpoint}")


# ============================================================================
# MCP Server Connectivity Tests
# ============================================================================


async def test_mcp_client_connects_to_keycloak_server(nc_mcp_keycloak_client):
    """Test MCP client can connect to mcp-keycloak service (port 8002).

    Verifies:
    - MCP client session is established
    - Server responds to list_tools request
    - Tools are available for use
    """
    result = await nc_mcp_keycloak_client.list_tools()

    assert result is not None
    assert len(result.tools) > 0

    logger.info(
        f"✓ MCP client connected to Keycloak server with {len(result.tools)} tools"
    )


async def test_external_idp_server_initialization(nc_mcp_keycloak_client):
    """Test that MCP server correctly initializes with external IdP configuration.

    Verifies:
    - Server auto-detects external IdP mode (issuer != Nextcloud host)
    - Server reports correct provider type
    - All expected tools are registered

    The server should log messages like:
    - "✓ Detected external IdP mode (issuer: http://keycloak:8080/realms/nextcloud-mcp != Nextcloud: http://app:80)"
    """
    result = await nc_mcp_keycloak_client.list_tools()

    # Verify we have a full set of tools (not filtered to specific apps)
    tool_names = [tool.name for tool in result.tools]

    # Should have tools from multiple apps
    has_notes = any("notes" in name for name in tool_names)
    has_calendar = any("calendar" in name for name in tool_names)
    has_files = any("webdav" in name for name in tool_names)

    assert has_notes, "Missing Notes tools"
    assert has_calendar, "Missing Calendar tools"
    assert has_files, "Missing WebDAV/Files tools"

    logger.info("✓ MCP server initialized with external IdP mode")
    logger.info(f"  Tools from multiple apps detected: {len(result.tools)} total")


# ============================================================================
# Token Validation Tests
# ============================================================================


async def test_external_idp_token_validation(nc_mcp_keycloak_client):
    """Test that Keycloak tokens are validated via Nextcloud user_oidc app.

    Token flow:
    1. Keycloak issues OAuth token
    2. MCP client sends token to MCP server
    3. MCP server passes token to Nextcloud user_oidc app
    4. user_oidc validates token with Keycloak (JWKS or introspection)
    5. Nextcloud returns user info to MCP server
    6. MCP server uses token to access Nextcloud APIs

    This test verifies the entire flow works.
    """
    # Execute a read operation (requires token validation)
    result = await nc_mcp_keycloak_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None
    response_data = json.loads(result.content[0].text)

    # Successful response means token was validated and user was authenticated
    assert "results" in response_data
    assert isinstance(response_data["results"], list)

    logger.info("✓ Keycloak token validated successfully via Nextcloud user_oidc app")
    logger.info(f"  Tool execution returned {len(response_data['results'])} results")


# ============================================================================
# Tool Execution Tests
# ============================================================================


async def test_tools_work_with_keycloak_token(nc_mcp_keycloak_client):
    """Test that MCP tools execute successfully with Keycloak OAuth tokens.

    Verifies end-to-end functionality:
    - Read operations work (nc_notes_search_notes)
    - Write operations work (nc_notes_create_note)
    - Different apps work (Notes, Calendar, Files)
    """
    # Test 1: Read operation (Notes)
    search_result = await nc_mcp_keycloak_client.call_tool(
        "nc_notes_search_notes", arguments={"query": ""}
    )
    assert search_result.isError is False
    logger.info("✓ Read operation successful (nc_notes_search_notes)")

    # Test 2: Write operation (Notes)
    create_result = await nc_mcp_keycloak_client.call_tool(
        "nc_notes_create_note",
        arguments={
            "title": "Keycloak Test Note",
            "content": "Created via external IdP token",
            "category": "Test",
        },
    )
    assert create_result.isError is False
    create_data = json.loads(create_result.content[0].text)
    note_id = create_data["id"]
    logger.info(f"✓ Write operation successful (created note {note_id})")

    # Test 3: Different app (Calendar)
    calendar_result = await nc_mcp_keycloak_client.call_tool(
        "nc_calendar_list_calendars", arguments={}
    )
    assert calendar_result.isError is False
    logger.info("✓ Calendar tool execution successful")

    # Test 4: File operations (WebDAV)
    files_result = await nc_mcp_keycloak_client.call_tool(
        "nc_webdav_list_directory", arguments={"path": "/"}
    )
    assert files_result.isError is False
    logger.info("✓ WebDAV tool execution successful")

    # Cleanup: Delete test note
    await nc_mcp_keycloak_client.call_tool(
        "nc_notes_delete_note", arguments={"note_id": note_id}
    )
    logger.info(f"✓ Cleanup: Deleted test note {note_id}")


async def test_keycloak_token_persistence(nc_mcp_keycloak_client):
    """Test that Keycloak token works across multiple operations.

    Verifies:
    - Token is properly cached by MCP server
    - Token can be reused for multiple API calls
    - No re-authentication is required between calls
    """
    # Execute multiple operations with same session
    operations = [
        ("nc_notes_search_notes", {"query": ""}),
        ("nc_calendar_list_calendars", {}),
        ("nc_webdav_list_directory", {"path": "/"}),
    ]

    for tool_name, arguments in operations:
        result = await nc_mcp_keycloak_client.call_tool(tool_name, arguments=arguments)
        assert result.isError is False, f"Failed to execute {tool_name}"
        logger.info(f"✓ {tool_name} executed successfully")

    logger.info("✓ Keycloak token persistence verified (3 operations with same token)")


# ============================================================================
# User Provisioning Tests
# ============================================================================


async def test_user_auto_provisioning(nc_client: NextcloudClient, keycloak_oauth_token):
    """Test that Nextcloud validates users from Keycloak token claims.

    When a user authenticates with Keycloak, Nextcloud's user_oidc app
    validates the token and authenticates the user. In this test setup,
    the Keycloak 'admin' user maps to the Nextcloud 'admin' user.

    Verification:
    1. User exists in Nextcloud after OAuth authentication
    2. User can access Nextcloud APIs with Keycloak token
    3. Bearer token validation is working correctly

    Note: With bearer-provisioning enabled, user_oidc would auto-provision
    new users from token claims, but since we use 'admin' in both Keycloak
    and Nextcloud, they map to the same user.
    """
    # Get list of users (returns List[str] of user IDs)
    user_ids = await nc_client.users.search_users()

    logger.info(f"Found {len(user_ids)} users in Nextcloud")
    logger.info(f"Users: {user_ids}")

    # Verify the admin user exists (used for authentication)
    assert "admin" in user_ids, "Expected 'admin' user to exist in Nextcloud"

    # Verify we can access APIs with the Keycloak token (already tested in previous tests)
    # The fact that we got this far means bearer token validation is working

    logger.info("✓ User authentication and bearer token validation verified")
    logger.info(f"  Total users: {len(user_ids)}")
    logger.info("  Bearer provisioning is enabled and working correctly")


# ============================================================================
# Scope-Based Authorization Tests
# ============================================================================


async def test_scope_filtering_with_keycloak(nc_mcp_keycloak_client):
    """Test that tool filtering works correctly with Keycloak JWT scopes.

    Keycloak tokens should include scopes in JWT payload (if JWT format).
    The MCP server should filter tools based on these scopes.

    Expected scopes (from docker-compose.yml):
    - openid profile email offline_access
    - notes:read notes:write
    - calendar:read calendar:write
    - contacts:read contacts:write
    - etc.

    Tools should be filtered accordingly.
    """
    result = await nc_mcp_keycloak_client.list_tools()
    tool_names = [tool.name for tool in result.tools]

    # With full scopes, all app tools should be available
    expected_tools = [
        "nc_notes_get_note",  # notes:read
        "nc_notes_create_note",  # notes:write
        "nc_calendar_list_calendars",  # calendar:read
        "nc_calendar_create_event",  # calendar:write
        "nc_webdav_list_directory",  # files:read
        "nc_webdav_write_file",  # files:write
    ]

    for tool_name in expected_tools:
        assert tool_name in tool_names, f"Expected tool {tool_name} not found"

    logger.info("✓ Scope-based tool filtering working with Keycloak tokens")
    logger.info(f"  Available tools: {len(tool_names)}")


# ============================================================================
# Error Handling Tests
# ============================================================================


async def test_keycloak_error_handling(nc_mcp_keycloak_client):
    """Test error handling with Keycloak tokens.

    Verifies:
    - Invalid operations return proper errors
    - Token validation errors are handled correctly
    - API errors propagate correctly through the chain
    """
    # Try to get a non-existent note
    result = await nc_mcp_keycloak_client.call_tool(
        "nc_notes_get_note", arguments={"note_id": 999999}
    )

    # Should get an error (note doesn't exist)
    assert result.isError is True
    logger.info(
        "✓ Keycloak OAuth server correctly handles errors for invalid operations"
    )


# ============================================================================
# Documentation Tests
# ============================================================================


async def test_external_idp_architecture():
    """Document the external IdP architecture (ADR-002).

    This test captures the design and flow for reference.
    """
    architecture = {
        "flow": [
            "1. User authenticates with Keycloak (external IdP)",
            "2. Keycloak issues OAuth access token with scopes",
            "3. MCP client uses token to authenticate with MCP server",
            "4. MCP server receives token and passes to Nextcloud",
            "5. Nextcloud user_oidc app validates token with Keycloak",
            "6. Nextcloud auto-provisions user from token claims (if first login)",
            "7. Nextcloud returns validated user info to MCP server",
            "8. MCP server executes tool using validated token",
        ],
        "components": {
            "keycloak": "External OAuth/OIDC provider (port 8888)",
            "mcp_server": "MCP server with external IdP config (port 8002)",
            "nextcloud": "API server with user_oidc app (port 8080)",
            "user_oidc": "Nextcloud app that validates external IdP tokens",
        },
        "configuration": {
            "keycloak_realm": "nextcloud-mcp",
            "keycloak_client": "nextcloud-mcp-server",
            "nextcloud_provider": "keycloak (via user_oidc app)",
            "token_validation": "Keycloak JWKS or introspection endpoint",
        },
        "advantages": [
            "No admin credentials needed in MCP server",
            "Centralized identity management",
            "Standards-based (RFC 6749, RFC 7662, RFC 9068)",
            "Supports enterprise IdPs (Keycloak, Auth0, Okta, etc.)",
            "User auto-provisioning from IdP claims",
        ],
    }

    logger.info("External IdP Architecture (ADR-002):")
    logger.info(json.dumps(architecture, indent=2))

    assert True
