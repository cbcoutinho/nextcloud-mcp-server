"""Tool failures must reach the model as tool results, not protocol errors.

This is the single behaviour most at risk in the mcp 1.x -> 2.x port. In 1.x the
SDK turned *every* exception out of a tool into ``CallToolResult(isError=True)``,
so the model read the message and could react. 2.x carves out ``MCPError``: it is
passed through as a top-level JSON-RPC error, which the client *raises* — the
model never sees it. Roughly 140 raise sites in this codebase are
argument-and-state failures ("Note 5 not found", "Nextcloud access not
provisioned") that must keep landing in the result.

``NextcloudMCPServer.call_tool`` maps ``MCPError`` back to ``ToolError`` for
exactly this reason. These tests pin that from the *client* side, over a real
``Client(server)`` connection (which negotiates 2026-07-28), because that is the
only vantage point where "raised at the caller" and "returned as content" differ.
"""

import httpx
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.errors import NextcloudMCPServer

pytestmark = pytest.mark.unit


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://nc/remote.php/dav/files/admin/notes/5.md")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )


def _server() -> NextcloudMCPServer:
    mcp = NextcloudMCPServer("tool-error-test")

    @mcp.tool()
    async def raises_mcp_error() -> str:
        """The shape ~140 of our tool raise sites use."""
        raise MCPError(code=-1, message="Note 5 not found")

    @mcp.tool()
    async def raises_nextcloud_404() -> str:
        """An unhandled Nextcloud HTTP failure escaping a tool."""
        raise _http_error(404)

    return mcp


async def test_mcp_error_lands_in_the_result_not_as_a_raise():
    async with Client(_server()) as client:
        result = await client.call_tool("raises_mcp_error", {})

    assert result.is_error is True, "MCPError escaped as a protocol error"
    assert "Note 5 not found" in str(result.content), (
        f"the model cannot see why the call failed: {result.content}"
    )


async def test_nextcloud_404_lands_in_the_result_with_a_usable_message():
    """2.x withholds the original text (``Error executing tool <name>``).

    ``friendly_tool_error`` reads it off ``__cause__`` instead, so the model
    still gets the resource, the status and what to do about it.
    """
    async with Client(_server()) as client:
        result = await client.call_tool("raises_nextcloud_404", {})

    assert result.is_error is True
    text = str(result.content)
    assert "Not found" in text, text
    assert "notes/5.md" in text, text
    # The bare SDK message, with nothing actionable in it, must not be what ships.
    assert text.strip() != "Error executing tool raises_nextcloud_404", text
