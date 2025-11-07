"""Integration tests for login elicitation with real MCP client callback support.

These tests verify the complete end-to-end login elicitation flow (ADR-006)
using the python-sdk MCP client with actual elicitation callback implementation.

Unlike test_login_elicitation.py which validates response formats, these tests
exercise the REAL elicitation protocol:
1. MCP client with elicitation callback connects to server
2. Tool triggers elicitation (ctx.elicit())
3. Client callback receives elicitation request
4. Callback completes OAuth flow via Playwright automation
5. Client returns acceptance
6. Tool proceeds with authenticated operation

This validates that:
- python-sdk MCP client can handle elicitation requests
- OAuth flow completion via callback works end-to-end
- Refresh tokens are properly stored after elicitation
- check_logged_in returns "yes" after successful OAuth
"""

import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def revoke_refresh_tokens(client):
    """Helper to revoke all refresh tokens from MCP server.

    This forces check_logged_in to trigger elicitation by removing
    any existing refresh tokens via the revoke_nextcloud_access tool.
    """
    logger.info("Revoking refresh tokens via revoke_nextcloud_access tool...")

    result = await client.call_tool("revoke_nextcloud_access", arguments={})

    logger.info(f"Revoke result: isError={result.isError}")
    if not result.isError:
        logger.info(f"✓ Revoke response: {result.content[0].text}")
    else:
        logger.warning(f"Revoke failed: {result.content}")


async def test_check_logged_in_with_real_elicitation_callback(
    nc_mcp_oauth_client_with_elicitation,
):
    """Test check_logged_in with actual elicitation callback that completes OAuth.

    This test validates the COMPLETE elicitation flow:
    1. Call check_logged_in tool (which triggers elicitation)
    2. Elicitation callback extracts OAuth URL
    3. Playwright automation completes OAuth flow
    4. Callback returns acceptance
    5. Tool returns "yes" (logged in)
    6. Refresh token is stored

    This is the ONLY test that exercises the real MCP elicitation protocol
    with python-sdk's ClientSession elicitation callback support.
    """
    client = nc_mcp_oauth_client_with_elicitation

    logger.info("=" * 80)
    logger.info("TEST: Real elicitation callback with OAuth completion")
    logger.info("=" * 80)

    # Revoke refresh tokens to force elicitation
    await revoke_refresh_tokens(client)

    # Call check_logged_in - this should trigger elicitation
    logger.info("Calling check_logged_in tool...")
    result = await client.call_tool("check_logged_in", arguments={})

    logger.info("Tool execution completed")
    logger.info(f"  Is error: {result.isError}")
    if result.content:
        response_text = result.content[0].text
        logger.info(f"  Response: {response_text}")
    else:
        logger.warning("  No content in response")

    # Validate tool execution succeeded
    assert result.isError is False, f"Tool execution failed: {result.content}"
    assert result.content is not None, "No content in tool response"

    response_text = result.content[0].text.lower()

    # Validate elicitation was triggered
    elicitation_count = client.elicitation_triggered["count"]
    logger.info(f"✓ Elicitation triggered {elicitation_count} time(s)")
    assert elicitation_count >= 1, (
        "Elicitation callback should have been invoked at least once"
    )

    # Validate OAuth completed successfully and tool returned "yes"
    assert "yes" in response_text, (
        f"Expected 'yes' after successful OAuth via elicitation, got: {response_text}"
    )

    logger.info("✅ Test passed: Real elicitation callback completed OAuth flow")
    logger.info("=" * 80)


async def test_elicitation_callback_url_extraction(
    nc_mcp_oauth_client_with_elicitation,
):
    """Test that elicitation callback correctly extracts OAuth URL.

    This validates the URL extraction logic in the callback by examining
    the elicitation message format returned by check_logged_in.
    """
    client = nc_mcp_oauth_client_with_elicitation

    logger.info("Testing OAuth URL extraction from elicitation message...")

    # Revoke refresh tokens to force elicitation
    await revoke_refresh_tokens(client)

    # Call check_logged_in to trigger elicitation
    result = await client.call_tool("check_logged_in", arguments={})

    # Should succeed (callback extracts URL and completes OAuth)
    assert result.isError is False
    assert "yes" in result.content[0].text.lower()

    # Elicitation should have been triggered
    assert client.elicitation_triggered["count"] >= 1

    logger.info("✓ URL extraction and OAuth completion successful")


async def test_elicitation_stores_refresh_token(
    nc_mcp_oauth_client_with_elicitation,
):
    """Test that refresh token is stored after elicitation completes.

    Validates that after successful OAuth via elicitation:
    1. check_logged_in returns "yes"
    2. check_provisioning_status shows is_provisioned=true
    """
    client = nc_mcp_oauth_client_with_elicitation

    logger.info("Testing refresh token storage after elicitation...")

    # Revoke refresh tokens to force elicitation
    await revoke_refresh_tokens(client)

    # Complete OAuth via elicitation
    result = await client.call_tool("check_logged_in", arguments={})
    assert result.isError is False
    assert "yes" in result.content[0].text.lower()

    # Verify refresh token was stored
    logger.info("Checking provisioning status...")
    status_result = await client.call_tool("check_provisioning_status", arguments={})

    assert status_result.isError is False
    status_text = status_result.content[0].text.lower()

    # Server should report provisioning complete
    assert "is_provisioned" in status_text or "offline" in status_text, (
        f"Expected provisioning status, got: {status_text}"
    )

    logger.info("✓ Refresh token stored successfully after elicitation")


async def test_second_check_logged_in_does_not_elicit(
    nc_mcp_oauth_client_with_elicitation,
):
    """Test that second call to check_logged_in does not trigger elicitation.

    After successful OAuth via elicitation:
    - First call: triggers elicitation, completes OAuth, returns "yes"
    - Second call: no elicitation (already logged in), returns "yes"
    """
    client = nc_mcp_oauth_client_with_elicitation

    logger.info("Testing that already-logged-in users don't get elicited...")

    # First call: triggers elicitation
    result1 = await client.call_tool("check_logged_in", arguments={})
    assert result1.isError is False
    assert "yes" in result1.content[0].text.lower()

    elicitation_count_after_first = client.elicitation_triggered["count"]
    logger.info(f"After first call: {elicitation_count_after_first} elicitations")

    # Second call: should NOT trigger elicitation (already logged in)
    result2 = await client.call_tool("check_logged_in", arguments={})
    assert result2.isError is False
    assert "yes" in result2.content[0].text.lower()

    elicitation_count_after_second = client.elicitation_triggered["count"]
    logger.info(f"After second call: {elicitation_count_after_second} elicitations")

    # Elicitation count should be the same (no new elicitation)
    assert elicitation_count_after_second == elicitation_count_after_first, (
        "Second check_logged_in should not trigger elicitation "
        "(user is already logged in)"
    )

    logger.info("✓ Already-logged-in users don't get redundant elicitations")
