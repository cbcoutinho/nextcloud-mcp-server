"""Date-only MCP queries use local calendar days, not UTC dates by accident."""

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import anyio
import httpx
import pytest
from caldav.lib.error import PropfindError
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.models.calendar import ListEventsResponse
from nextcloud_mcp_server.server.calendar import (
    _event_search_range,
    configure_calendar_tools,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "day,timezone,start,end",
    [
        ("2026-09-15", "Asia/Seoul", "2026-09-14T15:00:00Z", "2026-09-15T15:00:00Z"),
        (
            "2026-09-15",
            "Pacific/Kiritimati",
            "2026-09-14T10:00:00Z",
            "2026-09-15T10:00:00Z",
        ),
        (
            "2026-09-15",
            "America/Los_Angeles",
            "2026-09-15T07:00:00Z",
            "2026-09-16T07:00:00Z",
        ),
        ("2024-02-29", "UTC", "2024-02-29T00:00:00Z", "2024-03-01T00:00:00Z"),
        (
            "2026-03-08",
            "America/New_York",
            "2026-03-08T05:00:00Z",
            "2026-03-09T04:00:00Z",
        ),
        (
            "2026-11-01",
            "America/New_York",
            "2026-11-01T04:00:00Z",
            "2026-11-02T05:00:00Z",
        ),
    ],
)
def test_whole_local_days(day: str, timezone: str, start: str, end: str) -> None:
    assert _event_search_range(day, day, timezone) == (
        dt.datetime.fromisoformat(start),
        dt.datetime.fromisoformat(end),
    )


def test_default_utc_and_unbounded_dates() -> None:
    assert _event_search_range("", "") == (None, None)
    assert _event_search_range("2026-09-15", "") == (
        dt.datetime(2026, 9, 15, tzinfo=dt.UTC),
        None,
    )
    assert _event_search_range("", "2026-09-15") == (
        None,
        dt.datetime(2026, 9, 16, tzinfo=dt.UTC),
    )


def test_iso_datetimes_preserve_explicit_offsets_and_exact_end() -> None:
    assert _event_search_range(
        "2026-09-15T00:00:00+09:00", "2026-09-15T00:00:01.5+09:00", "America/New_York"
    ) == (
        dt.datetime(2026, 9, 14, 15, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 14, 15, 0, 1, 500000, tzinfo=dt.UTC),
    )
    assert _event_search_range("2026-09-15T00:00:00", "", "Asia/Seoul")[0] == (
        dt.datetime(2026, 9, 14, 15, tzinfo=dt.UTC)
    )


def test_seoul_midnight_and_final_fractional_second_are_included() -> None:
    start, end = _event_search_range("2026-09-15", "2026-09-15", "Asia/Seoul")
    tz = ZoneInfo("Asia/Seoul")
    assert start is not None and end is not None
    for event in (
        dt.datetime(2026, 9, 15, tzinfo=tz),
        dt.datetime(2026, 9, 15, 3, 5, tzinfo=tz),
        dt.datetime(2026, 9, 15, 23, 59, 59, 999999, tzinfo=tz),
    ):
        assert start <= event < end
    assert end == dt.datetime(2026, 9, 16, tzinfo=tz)


@pytest.fixture
def listing(mocker):
    mcp = MCPServer("calendar-query-test")
    configure_calendar_tools(mcp)
    client = mocker.MagicMock()
    event = {
        "uid": "early",
        "title": "Early",
        "start_datetime": "2026-09-15T03:05:00+09:00",
    }
    client.calendar.get_calendar_events = mocker.AsyncMock(
        return_value=[event.copy(), event.copy()]
    )
    client.calendar.get_calendar_display_name = mocker.AsyncMock(
        return_value="My Calendar"
    )
    client.calendar.search_events_across_calendars = mocker.AsyncMock(
        return_value=[
            {**event, "calendar_name": "1", "calendar_display_name": "My Calendar"}
        ]
    )
    get_client = mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client", return_value=client
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return mcp, client, get_client


@pytest.mark.parametrize("all_calendars", [False, True])
async def test_list_tool_forwards_utc_bounds_and_real_display_name(
    listing, mocker, all_calendars: bool
) -> None:
    mcp, client, _ = listing
    tool = mcp._tool_manager.get_tool("nc_calendar_list_events")
    result = await tool.fn(
        ctx=mocker.MagicMock(),
        calendar_name="1",
        start_date="2026-09-15",
        end_date="2026-09-16",
        timezone="Asia/Seoul",
        search_all_calendars=all_calendars,
    )
    assert isinstance(result, ListEventsResponse)
    query = (
        client.calendar.search_events_across_calendars
        if all_calendars
        else client.calendar.get_calendar_events
    )
    assert query.call_args.kwargs["start_datetime"] == dt.datetime(
        2026, 9, 14, 15, tzinfo=dt.UTC
    )
    assert query.call_args.kwargs["end_datetime"] == dt.datetime(
        2026, 9, 16, 15, tzinfo=dt.UTC
    )
    assert all(
        e.calendar_name == "1" and e.calendar_display_name == "My Calendar"
        for e in result.events
    )
    assert result.calendar_name == (None if all_calendars else "1")
    assert result.start_date == "2026-09-15"
    assert result.end_date == "2026-09-16"
    if all_calendars:
        client.calendar.get_calendar_display_name.assert_not_awaited()
    else:
        client.calendar.get_calendar_display_name.assert_awaited_once_with("1")
    client.calendar.list_calendars.assert_not_called()


@pytest.mark.parametrize(
    "error_type", [httpx.TransportError, PropfindError, ValueError]
)
async def test_display_name_failure_preserves_events(
    listing, mocker, caplog, error_type: type[Exception]
) -> None:
    mcp, client, _ = listing
    client.calendar.get_calendar_display_name.side_effect = error_type(
        "sensitive DAV error detail"
    )
    caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.server.calendar")
    tool = mcp._tool_manager.get_tool("nc_calendar_list_events")
    result = await tool.fn(ctx=mocker.MagicMock(), calendar_name="1")

    assert result.success is True
    assert result.total_found == 2
    assert all(e.uid == "early" and e.summary == "Early" for e in result.events)
    assert all(
        e.calendar_name == "1" and e.calendar_display_name == "1" for e in result.events
    )
    client.calendar.get_calendar_display_name.assert_awaited_once_with("1")
    assert "using calendar slug" in caplog.text
    assert "sensitive DAV error detail" not in caplog.text


@pytest.mark.parametrize("filtered", [False, True])
async def test_empty_results_skip_display_name_lookup(
    listing, mocker, filtered: bool
) -> None:
    mcp, client, _ = listing
    if filtered:
        client.calendar._apply_event_filters.return_value = []
    else:
        client.calendar.get_calendar_events.return_value = []
    tool = mcp._tool_manager.get_tool("nc_calendar_list_events")
    result = await tool.fn(
        ctx=mocker.MagicMock(),
        calendar_name="1",
        title_contains="no match" if filtered else None,
    )

    assert result.events == []
    assert result.total_found == 0
    client.calendar.get_calendar_display_name.assert_not_awaited()


async def test_display_name_cancellation_propagates(listing, mocker) -> None:
    mcp, client, _ = listing
    cancelled = anyio.get_cancelled_exc_class()
    client.calendar.get_calendar_display_name.side_effect = cancelled()
    tool = mcp._tool_manager.get_tool("nc_calendar_list_events")
    with pytest.raises(cancelled):
        await tool.fn(ctx=mocker.MagicMock(), calendar_name="1")


@pytest.mark.parametrize("all_calendars", [False, True])
@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"start_date": "not-a-date"}, "start_date"),
        ({"end_date": "2026-02-29"}, "end_date"),
        ({"end_date": "9999-12-31"}, "end_date"),
        ({"timezone": "Asia/Seoull"}, "timezone"),
        ({"timezone": ""}, "timezone"),
        ({"timezone": "/etc/localtime"}, "timezone"),
        ({"start_date": "2026-09-16", "end_date": "2026-09-15"}, "start_date"),
        (
            {"start_date": "2026-09-15T00:00:00Z", "end_date": "2026-09-15T00:00:00Z"},
            "start_date",
        ),
    ],
)
async def test_bad_bounds_fail_before_client_access(
    listing, mocker, all_calendars: bool, kwargs: dict, message: str
) -> None:
    mcp, _, get_client = listing
    tool = mcp._tool_manager.get_tool("nc_calendar_list_events")
    with pytest.raises(ToolError, match=message):
        await tool.fn(
            ctx=mocker.MagicMock(),
            calendar_name="1",
            search_all_calendars=all_calendars,
            **kwargs,
        )
    get_client.assert_not_called()


async def test_mcp_schema_exposes_optional_timezone_and_typed_output(listing) -> None:
    mcp, _, _ = listing
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "nc_calendar_list_events")
    assert tool.input_schema["properties"]["timezone"] == {
        "default": "UTC",
        "title": "Timezone",
        "type": "string",
    }
    assert "timezone" not in tool.input_schema.get("required", [])
    assert "events" in tool.output_schema["properties"]


async def test_mcp_round_trip_preserves_calendar_context_and_reports_bad_input(
    listing,
) -> None:
    mcp, _, _ = listing
    async with Client(mcp) as client:
        response = await client.call_tool(
            "nc_calendar_list_events",
            {
                "calendar_name": "1",
                "start_date": "2026-09-15",
                "end_date": "2026-09-15",
                "timezone": "Asia/Seoul",
            },
        )
        error = await client.call_tool(
            "nc_calendar_list_events",
            {
                "calendar_name": "1",
                "start_date": "2026-02-30",
            },
        )
    assert response.is_error is False
    data = json.loads(response.content[0].text)
    assert data["success"] is True
    assert data["events"][0]["calendar_name"] == "1"
    assert data["events"][0]["calendar_display_name"] == "My Calendar"
    assert error.is_error is True
    assert "Invalid start_date" in str(error.content)
