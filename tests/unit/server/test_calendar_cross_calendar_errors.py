"""Cross-calendar tools report the calendars they could not read.

A calendar that fails to load used to be logged and dropped, so the response
could not distinguish "no events" from "calendar could not be read" -- which is
how #1449 went unnoticed. The client already collects those failures; these pin
that every cross-calendar tool surfaces them as ``errors``.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit

FAILURE = {"calendar_name": "shared_by_someone", "error": "calendar is on fire"}


@pytest.fixture
def calendar_tools():
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    return mcp._tool_manager


@pytest.fixture
def stub_client(mocker):
    """A client whose cross-calendar searches skip one calendar."""

    async def search(*args, failures=None, **kwargs):
        failures.append(dict(FAILURE))
        return []

    client = mocker.MagicMock()
    client.calendar.search_events_across_calendars = mocker.AsyncMock(
        side_effect=search
    )
    client.calendar.search_todos_across_calendars = mocker.AsyncMock(side_effect=search)
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("nc_calendar_list_events", {"search_all_calendars": True}),
        ("nc_calendar_get_upcoming_events", {}),
        ("nc_calendar_search_todos", {}),
    ],
)
async def test_skipped_calendar_is_reported_in_errors(
    calendar_tools, stub_client, mocker, tool_name, kwargs
):
    result = await calendar_tools.get_tool(tool_name).fn(
        ctx=mocker.MagicMock(), **kwargs
    )

    assert [e.model_dump() for e in result.errors] == [FAILURE]
