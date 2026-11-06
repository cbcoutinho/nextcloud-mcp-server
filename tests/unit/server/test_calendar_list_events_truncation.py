"""nc_calendar_list_events reports when ``limit`` cut the listing (GH #1774).

``total_found`` counts what was returned, so a cut listing used to look
complete. The tool now asks for one event past the limit and reports a surplus
as ``truncated``.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit


def _events(n: int) -> list[dict]:
    return [
        {"uid": f"e{i}", "title": f"E{i}", "start_datetime": f"2026-09-{i + 1:02d}"}
        for i in range(n)
    ]


@pytest.fixture
def list_events(mocker):
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    client = mocker.MagicMock()
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    fn = mcp._tool_manager.get_tool("nc_calendar_list_events").fn

    async def call(available: int, **kwargs):
        # The stub honours the limit it is asked for, as the client does.
        async def fetch(*args, limit, **kw):
            return _events(available)[:limit]

        client.calendar.get_calendar_events = mocker.AsyncMock(side_effect=fetch)
        client.calendar.search_events_across_calendars = mocker.AsyncMock(
            side_effect=fetch
        )
        return await fn(ctx=mocker.MagicMock(), **kwargs)

    return call


@pytest.mark.parametrize(
    "scope", [{"calendar_name": "personal"}, {"search_all_calendars": True}]
)
@pytest.mark.parametrize(("available", "truncated"), [(3, False), (4, True)])
async def test_truncated_flags_a_cut_listing(list_events, scope, available, truncated):
    result = await list_events(available, limit=3, **scope)

    assert result.truncated is truncated
    assert result.total_found == 3
    assert [e.uid for e in result.events] == ["e0", "e1", "e2"]


async def test_negative_limit_is_rejected(list_events):
    with pytest.raises(ToolError, match="limit"):
        await list_events(3, limit=-1, calendar_name="personal")
