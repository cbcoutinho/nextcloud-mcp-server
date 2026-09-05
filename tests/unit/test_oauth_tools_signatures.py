"""Unit tests for OAuth tool input-schema hardening (issue #626 finding 3).

These tools must derive `user_id` from the verified MCP access token and
must never accept it as an MCP-level input. Otherwise an LLM (or any MCP
client) could supply an arbitrary user_id and reach cross-user revoke or
status-disclosure operations.
"""

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.server.oauth_tools import register_oauth_tools

pytestmark = pytest.mark.unit


HARDENED_TOOLS = (
    "provision_nextcloud_access",
    "revoke_nextcloud_access",
    "check_provisioning_status",
    "check_logged_in",
)


@pytest.fixture
def registered_tools():
    """Register the OAuth tools against a fresh MCPServer and return them by name.

    Uses MCPServer's `_tool_manager.list_tools()`; flagged as internal and may
    break on SDK upgrades, but this is the supported way to inspect a tool's
    JSON input schema in unit tests (see tests/unit/test_stdio.py).
    """
    mcp = MCPServer("test-oauth-tools")
    register_oauth_tools(mcp)
    tools = mcp._tool_manager.list_tools()
    return {t.name: t for t in tools}


def test_oauth_tools_registered(registered_tools):
    for name in HARDENED_TOOLS:
        assert name in registered_tools, f"{name} should be registered"


@pytest.mark.parametrize("tool_name", HARDENED_TOOLS)
def test_oauth_tool_schema_does_not_accept_user_id(tool_name, registered_tools):
    """user_id must not appear in the tool's JSON input schema."""
    tool = registered_tools[tool_name]
    properties = tool.parameters.get("properties", {})
    required = tool.parameters.get("required", [])

    assert "user_id" not in properties, (
        f"{tool_name} accepts user_id as an MCP input — must be derived from "
        f"the verified access token (issue #626 finding 3). "
        f"properties={list(properties.keys())}"
    )
    assert "user_id" not in required
