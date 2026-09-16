"""Regression tests for GH #568: list-returning tools must emit one content block.

FastMCP serialises a bare Python list into one ``TextContent`` block per element,
so an MCP client that reads only ``content[0]`` (mcporter, Claude Code via
mcp-remote) sees a single item instead of the whole collection. Tools that wrap
their result in a Response object serialise as one block and are unaffected.

The dedicated list tools were consolidated onto the Response pattern, but two
paths still returned a raw list: ``nc_calendar_manage_calendar(action="list")``
(``nc_calendar_list_calendars`` already wraps the identical call) and
``nc_tables_read_table``.
"""

from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult

from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.server.calendar import configure_calendar_tools
from nextcloud_mcp_server.server.tables import configure_tables_tools

pytestmark = pytest.mark.unit


def _blocks(result):
    """The content blocks of a ``call_tool`` result, whichever shape the SDK returns."""
    if isinstance(result, CallToolResult):
        return result.content
    if isinstance(result, tuple):
        return result[0]
    return result


@pytest.fixture(autouse=True)
def _allow_scopes(mocker):
    # @require_scopes denies a context-bearing request without a verified token
    # only under login-flow; pin it off so the tools run in the unit harness.
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )


async def test_manage_calendar_list_returns_single_block(mocker):
    client = mocker.MagicMock()
    client.calendar.list_calendars = mocker.AsyncMock(
        return_value=[
            {"name": "personal", "display_name": "Personal"},
            {"name": "work", "display_name": "Work"},
            {"name": "family", "display_name": "Family"},
        ]
    )
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )

    mcp = NextcloudMCPServer("test")
    configure_calendar_tools(mcp)

    blocks = _blocks(
        await mcp.call_tool("nc_calendar_manage_calendar", {"action": "list"})
    )

    assert len(blocks) == 1
    payload = json.loads(blocks[0].text)
    assert [cal["name"] for cal in payload["calendars"]] == [
        "personal",
        "work",
        "family",
    ]


async def test_read_table_returns_single_block(mocker):
    rows = [
        {"id": 1, "tableId": 7, "data": [{"columnId": 1, "value": "a"}]},
        {"id": 2, "tableId": 7, "data": [{"columnId": 1, "value": "b"}]},
        {"id": 3, "tableId": 7, "data": [{"columnId": 1, "value": "c"}]},
    ]
    client = mocker.MagicMock()
    client.tables.get_table_rows = mocker.AsyncMock(return_value=rows)
    mocker.patch(
        "nextcloud_mcp_server.server.tables.get_client",
        mocker.AsyncMock(return_value=client),
    )

    mcp = NextcloudMCPServer("test")
    configure_tables_tools(mcp)

    blocks = _blocks(await mcp.call_tool("nc_tables_read_table", {"table_id": 7}))

    assert len(blocks) == 1
    payload = json.loads(blocks[0].text)
    assert payload["table_id"] == 7
    assert [row["id"] for row in payload["rows"]] == [1, 2, 3]
