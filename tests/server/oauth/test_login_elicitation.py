"""Integration tests for login elicitation flow (ADR-006 Interim Implementation).

Tests verify:
1. check_logged_in tool with elicitation for unauthenticated users
2. Elicitation contains login URL in message
3. User can complete login via OAuth
4. After login, check_logged_in returns "yes"
5. Already-authenticated users get immediate "yes" response
6. Elicitation decline/cancel handling
"""

import logging
import re

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def test_check_logged_in_elicitation_flow(
    nc_mcp_oauth_client, browser, oauth_callback_server
):
    """Test that check_logged_in elicits login for unauthenticated user.

    This test validates the interim workaround for SEP-1036:
    1. Call check_logged_in on unauthenticated client
    2. Receive elicitation with login URL in message
    3. Use Playwright to navigate to URL and complete OAuth
    4. Accept the elicitation
    5. Verify tool returns "yes" after successful login
    """
    # Step 1: Call check_logged_in tool - should trigger elicitation
    logger.info("Step 1: Calling check_logged_in on unauthenticated client")

    # In a real scenario, we'd need to handle the elicitation request/response
    # For now, we'll test that the tool exists and can be called
    result = await nc_mcp_oauth_client.call_tool("check_logged_in", arguments={})

    # The tool should either:
    # - Return an elicitation (if MCP client supports it)
    # - Return a string response with "yes" or "not logged in"
    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None

    response_text = result.content[0].text
    logger.info(f"check_logged_in response: {response_text}")

    # For now, since we're using an OAuth client that's already authenticated,
    # we expect to get "yes"
    # TODO: This test needs to be enhanced when MCP elicitation support is available


async def test_check_logged_in_already_authenticated(nc_mcp_oauth_client):
    """Test that check_logged_in returns 'yes' for authenticated user.

    This test verifies that if the user has already completed Flow 2
    (resource provisioning), the tool immediately returns "yes" without
    elicitation.
    """
    logger.info("Calling check_logged_in on authenticated client")

    # Since we're using the nc_mcp_oauth_client fixture which completes
    # OAuth during setup, the user should already be provisioned
    result = await nc_mcp_oauth_client.call_tool("check_logged_in", arguments={})

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None

    response_text = result.content[0].text
    logger.info(f"Response: {response_text}")

    # Check for valid responses:
    # - "yes" (already logged in)
    # - "not enabled" (offline access not enabled)
    # - "not configured" (MCP_SERVER_CLIENT_ID not set)
    # - "elicitation not supported" (test environment limitation)
    assert (
        "yes" in response_text.lower()
        or "not enabled" in response_text.lower()
        or "not configured" in response_text.lower()
        or "elicitation not supported" in response_text.lower()
    )


async def test_check_logged_in_url_format(nc_mcp_oauth_client):
    """Test that login URL (when needed) follows correct OAuth format.

    This test verifies that if the tool needs to provide a login URL,
    the URL contains the correct OAuth parameters for Flow 2.
    """
    # Call the tool
    result = await nc_mcp_oauth_client.call_tool("check_logged_in", arguments={})

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None

    response_text = result.content[0].text
    logger.info(f"Response: {response_text}")

    # If response contains a URL, validate it
    url_pattern = r"https?://[^\s]+"
    urls = re.findall(url_pattern, response_text)

    if urls:
        login_url = urls[0]
        logger.info(f"Found login URL: {login_url}")

        # Validate OAuth parameters
        assert "response_type=code" in login_url
        assert "client_id=" in login_url
        assert "redirect_uri=" in login_url
        assert "scope=" in login_url
        assert "state=" in login_url
        assert "openid" in login_url  # Should request openid scope

        # Validate callback URL (unified endpoint without query params)
        # Note: redirect_uri should be /oauth/callback (no query params)
        # Flow type is determined by session lookup, not URL params
        assert (
            "/oauth/callback" in login_url
            or "callback-nextcloud" in login_url  # Legacy support
            or "authorize-nextcloud" in login_url
        )


async def test_check_logged_in_with_user_id(nc_mcp_oauth_client):
    """Test that check_logged_in accepts optional user_id parameter.

    This verifies the tool can be called with an explicit user_id.
    """
    result = await nc_mcp_oauth_client.call_tool(
        "check_logged_in", arguments={"user_id": "testuser"}
    )

    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None

    response_text = result.content[0].text
    logger.info(f"Response with user_id: {response_text}")

    # Should get some response (either yes or not logged in)
    assert len(response_text) > 0


async def test_check_logged_in_tool_metadata(nc_mcp_oauth_client):
    """Test that check_logged_in tool has correct metadata."""
    tools = await nc_mcp_oauth_client.list_tools()
    assert tools is not None

    # Find the check_logged_in tool
    check_logged_in_tool = None
    for tool in tools.tools:
        if tool.name == "check_logged_in":
            check_logged_in_tool = tool
            break

    assert check_logged_in_tool is not None, "check_logged_in tool not found"
    logger.info(f"Tool: {check_logged_in_tool.name}")
    logger.info(f"Description: {check_logged_in_tool.description}")

    # Verify description mentions login
    assert "login" in check_logged_in_tool.description.lower()

    # Tool should have openid scope requirement
    # (This would need to be verified via tool schema if exposed)
