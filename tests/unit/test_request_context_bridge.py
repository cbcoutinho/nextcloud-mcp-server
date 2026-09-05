"""The per-request Context bridge, driven through the real mcp 2.x dispatcher.

mcp 2.x removed ``request_ctx`` and ``MCPServer.get_context()``, and injects
``Context`` only into tool and *template*-resource functions. Three things here
still need one — capability gating in ``list_tools()``, static
``@mcp.resource()`` functions, and ``enforce_capability`` on ``tools/call`` — so
:class:`NextcloudMCPServer` republishes it from a middleware.

These go through ``Client(server)`` rather than calling the methods directly,
because the middleware only runs on the dispatcher path: a direct
``mcp.list_tools()`` would pass while the served one silently fails open.
``Client(server)`` also negotiates 2026-07-28, so this pins the bridge on the
era the upgrade is about.
"""

import json

import pytest
from mcp.client import Client

from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.request_context import current_context

pytestmark = pytest.mark.unit


def _server() -> NextcloudMCPServer:
    mcp = NextcloudMCPServer("bridge-test")

    @mcp.resource("nc://static")
    async def static_resource() -> dict:
        """A static resource: the SDK refuses to inject Context into these."""
        ctx = current_context(mcp)
        return {"request_id": str(ctx.request_id), "method": ctx.request_context.method}

    @mcp.tool()
    async def peek() -> str:
        """A tool, to prove the bridge is live on tools/call too."""
        return current_context(mcp).request_context.method

    return mcp


async def test_static_resource_reaches_the_request_context():
    async with Client(_server()) as client:
        result = await client.read_resource("nc://static")

    payload = json.loads(result.contents[0].text)
    assert payload["method"] == "resources/read"
    assert payload["request_id"]


async def test_tool_call_reaches_the_request_context():
    async with Client(_server()) as client:
        result = await client.call_tool("peek", {})

    assert result.content[0].text == "tools/call"


async def test_list_tools_reaches_the_request_context():
    """``list_tools()`` takes no context, so capability gating depends on this."""
    seen: list[str] = []

    mcp = _server()
    original = mcp.list_tools

    async def recording_list_tools():
        seen.append(current_context(mcp).request_context.method)
        return await original()

    mcp.list_tools = recording_list_tools  # type: ignore[method-assign]

    async with Client(mcp) as client:
        await client.list_tools()

    assert seen == ["tools/list"]


async def test_context_is_cleared_between_requests():
    """A leaked contextvar would let one request read another's context."""
    async with Client(_server()) as client:
        await client.call_tool("peek", {})

    with pytest.raises(LookupError):
        current_context()
