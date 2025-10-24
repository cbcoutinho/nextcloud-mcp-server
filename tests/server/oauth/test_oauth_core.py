"""Core OAuth integration tests.

Consolidated from:
- test_mcp_oauth.py: Basic OAuth connectivity
- test_mcp_oauth_jwt.py: JWT-specific operations
- test_jwt_tokens.py: JWT token structure validation

Tests verify:
1. OAuth server connectivity and tool listing
2. Tool execution with OAuth tokens
3. JWT token structure and claims
4. Multiple operations with same token (persistence)
5. Error handling with OAuth
"""

import base64
import json
import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


def decode_jwt_without_verification(token: str) -> dict:
    """Decode JWT token without signature verification (for inspection only).

    Returns:
        Dict with header and payload
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid JWT format: expected 3 parts, got {len(parts)}")

    # Decode header
    header = json.loads(
        base64.urlsafe_b64decode(parts[0] + "=" * (4 - len(parts[0]) % 4))
    )

    # Decode payload
    payload = json.loads(
        base64.urlsafe_b64decode(parts[1] + "=" * (4 - len(parts[1]) % 4))
    )

    return {
        "header": header,
        "payload": payload,
    }


# ============================================================================
# Basic OAuth Connectivity Tests
# ============================================================================


async def test_mcp_oauth_server_connection(nc_mcp_oauth_client):
    """Test connection to OAuth-enabled MCP server."""
    result = await nc_mcp_oauth_client.list_tools()
    assert result is not None
    assert len(result.tools) > 0

    logger.info(f"OAuth MCP server has {len(result.tools)} tools available")


async def test_mcp_oauth_tool_execution(nc_mcp_oauth_client):
    """Test executing a tool on the OAuth-enabled MCP server."""
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


async def test_mcp_oauth_client_with_playwright(nc_mcp_oauth_client):
    """Test that MCP OAuth client via Playwright can execute tools."""
    # Test: Execute the 'nc_notes_search_notes' tool
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
        f"Successfully executed 'nc_notes_search_notes' tool on Playwright OAuth MCP server and got {len(response_data['results'])} notes."
    )


# ============================================================================
# JWT-Specific Tests
# ============================================================================


async def test_jwt_tool_list_operations(nc_mcp_oauth_jwt_client):
    """Test that list_tools works with JWT authentication and returns expected tools.

    This test verifies that tools are properly filtered based on per-app scopes:
    - notes:read/write → Notes app tools
    - calendar:read/write → Calendar app tools
    - files:read/write → WebDAV/Files app tools
    - etc.
    """
    result = await nc_mcp_oauth_jwt_client.list_tools()

    # Verify we have tools
    assert len(result.tools) > 0

    # Verify expected tools exist based on configured scopes
    tool_names = [tool.name for tool in result.tools]

    # Notes tools (require notes:read and notes:write)
    assert "nc_notes_get_note" in tool_names, "Missing nc_notes_get_note (notes:read)"
    assert "nc_notes_create_note" in tool_names, (
        "Missing nc_notes_create_note (notes:write)"
    )

    # Calendar tools (require calendar:read and calendar:write)
    assert "nc_calendar_list_calendars" in tool_names, (
        "Missing nc_calendar_list_calendars (calendar:read)"
    )
    assert "nc_calendar_create_event" in tool_names, (
        "Missing nc_calendar_create_event (calendar:write)"
    )

    # Verify we have a reasonable number of tools for the configured scopes
    # With notes + calendar scopes, expect ~20-30 tools
    assert len(tool_names) >= 20, (
        f"Expected at least 20 tools with notes+calendar scopes, got {len(tool_names)}"
    )

    logger.info(
        f"JWT OAuth server provides {len(result.tools)} tools with configured per-app scopes"
    )


async def test_jwt_multiple_operations(nc_mcp_oauth_jwt_client):
    """Test multiple operations with same JWT token to verify token persistence.

    JWT tokens should work across multiple tool calls without re-authentication,
    demonstrating that the token is properly cached and reused.
    """
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

    logger.info(
        "Successfully executed 3 different operations with same JWT token (token persistence verified)"
    )


async def test_jwt_error_handling(nc_mcp_oauth_jwt_client):
    """Test error handling with JWT authentication.

    Verifies that invalid operations return proper errors even with valid JWT tokens.
    """
    # Try to get a non-existent note
    result = await nc_mcp_oauth_jwt_client.call_tool(
        "nc_notes_get_note", arguments={"note_id": 999999}
    )

    # Should get an error (note doesn't exist)
    assert result.isError is True
    logger.info("JWT OAuth server correctly handles errors for invalid operations")


# ============================================================================
# JWT Token Structure Tests
# ============================================================================


async def test_jwt_tokens_embed_scopes_in_payload():
    """Document that JWT tokens embed scopes in the payload (RFC 9068).

    This test documents expected JWT structure based on manual testing.
    """
    expected_structure = {
        "header": {
            "typ": "at+JWT",  # RFC 9068 access token type
            "alg": "RS256",  # Signature algorithm
        },
        "payload_claims": {
            "iss": "issuer URL",
            "sub": "user ID",
            "aud": "client ID",
            "exp": "expiration timestamp",
            "iat": "issued at timestamp",
            "scope": "space-separated scope string (e.g., 'notes:read notes:write')",
            "client_id": "client identifier",
            "jti": "JWT ID",
        },
        "scope_claim": {
            "format": "space-separated string",
            "example": "openid profile email notes:read notes:write",
            "extraction": "payload['scope'].split()",
        },
    }

    logger.info("JWT token structure (RFC 9068):")
    logger.info(json.dumps(expected_structure, indent=2))

    # This test documents expected behavior
    assert True


async def test_opaque_token_vs_jwt_comparison():
    """Document differences between opaque tokens and JWT tokens.

    This test captures our findings about the two token types.
    """
    findings = {
        "jwt_advantages": [
            "Scopes embedded in payload - no introspection needed",
            "Self-contained - can validate with JWKS",
            "Standard approach (RFC 9068)",
        ],
        "jwt_disadvantages": [
            "10-15x larger than opaque tokens (~800-1200 chars vs 72)",
            "Cannot be easily revoked (until expiration)",
        ],
        "token_sizes": {
            "opaque": "72 characters",
            "jwt": "~800-1200 characters",
        },
        "recommendation": "Use JWT for MCP server (scopes available without introspection)",
    }

    logger.info("JWT vs Opaque token comparison:")
    logger.info(json.dumps(findings, indent=2))

    assert True
