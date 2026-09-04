"""The server served over a real 2026-07-28 Streamable HTTP connection.

Every other test here drives a hand-rolled ``ClientSession`` + ``initialize()``,
which negotiates a **2025-era** protocol version — the same thing today's real
clients do, and worth keeping. But it means nothing else in the suite exercises
2026-07-28 over the wire, which is the revision this SDK upgrade is about.

``Client(url)`` defaults to ``mode="auto"``, so it negotiates the newest version
both ends speak. Connecting at all proves the handshake (``server/discover``,
no session), and a tool call proves the middleware chain, capability gating and
result conversion all work on that path.
"""

import os

import pytest
from mcp.client import Client

pytestmark = [pytest.mark.integration, pytest.mark.smoke]

MCP_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")


async def test_negotiates_2026_and_serves_tools():
    async with Client(MCP_URL) as client:
        assert client.protocol_version >= "2026-07-28", (
            f"expected a 2026-era connection, negotiated {client.protocol_version}"
        )

        tools = await client.list_tools()
        assert len(tools.tools) > 30, f"Expected >30 tools, got {len(tools.tools)}"

        # A read-only tool with no arguments, so this asserts the call path
        # rather than any particular Nextcloud state.
        result = await client.call_tool("nc_calendar_list_calendars", {})
        assert result.is_error is False, result.content
