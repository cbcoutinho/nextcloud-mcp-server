"""Bulk updates must carry a requested calendar scope through to the client."""

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit


async def test_bulk_update_forwards_calendar_name_as_filter(mocker):
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    client = mocker.MagicMock()
    client.calendar.bulk_update_events = mocker.AsyncMock(
        return_value={"updated_count": 0}
    )
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )

    await mcp._tool_manager.get_tool("nc_calendar_bulk_operations").fn(
        ctx=mocker.MagicMock(),
        operation="update",
        calendar_name="personal",
        new_title="Renamed",
    )

    client.calendar.bulk_update_events.assert_awaited_once_with(
        {"calendar_name": "personal"},
        {"title": "Renamed"},
        apply_to_series=False,
    )
