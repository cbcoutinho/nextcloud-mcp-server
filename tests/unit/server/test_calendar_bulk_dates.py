"""Bulk mutations share the listing tool's local-day search contract."""

import datetime as dt
import json
from typing import Any

import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from nextcloud_client.calendar import CalendarClient

from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit


@pytest.fixture
def bulk(mocker):
    mcp = MCPServer("calendar-bulk-date-test")
    configure_calendar_tools(mcp)
    calendar = mocker.MagicMock()
    calendar._ensure_calendar_home = mocker.AsyncMock()
    calendar.get_calendar_events = mocker.AsyncMock(return_value=[])
    calendar.search_events_across_calendars = mocker.AsyncMock(return_value=[])
    calendar._bulk_targets.side_effect = CalendarClient._bulk_targets
    calendar.update_event = mocker.AsyncMock()
    calendar.delete_event = mocker.AsyncMock(return_value={"success": True})
    calendar.create_event = mocker.AsyncMock()

    async def update(
        filters: dict[str, Any],
        data: dict[str, Any],
        *,
        apply_to_series: bool = False,
    ) -> dict[str, Any]:
        return await CalendarClient.bulk_update_events(
            calendar, filters, data, apply_to_series=apply_to_series
        )

    calendar.bulk_update_events = mocker.AsyncMock(side_effect=update)
    get_client = mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        return_value=mocker.MagicMock(calendar=calendar),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    tool = mcp._tool_manager.get_tool("nc_calendar_bulk_operations")
    return mcp, tool, calendar, get_client


@pytest.mark.parametrize("operation", ["update", "delete", "move"])
@pytest.mark.parametrize("calendar_name", [None, "personal"])
@pytest.mark.parametrize(
    "dates,start,end",
    [
        (
            {
                "start_date": "2026-09-15",
                "end_date": "2026-09-15",
                "timezone": "Asia/Seoul",
            },
            "2026-09-14T15:00:00+00:00",
            "2026-09-15T15:00:00+00:00",
        ),
        (
            {
                "start_date": "2026-03-08",
                "end_date": "2026-03-08",
                "timezone": "America/New_York",
            },
            "2026-03-08T05:00:00+00:00",
            "2026-03-09T04:00:00+00:00",
        ),
        (
            {
                "start_date": "2026-11-01",
                "end_date": "2026-11-01",
                "timezone": "America/New_York",
            },
            "2026-11-01T04:00:00+00:00",
            "2026-11-02T05:00:00+00:00",
        ),
        (
            {"start_date": "2026-09-15", "end_date": "2026-09-15"},
            "2026-09-15T00:00:00+00:00",
            "2026-09-16T00:00:00+00:00",
        ),
        (
            {
                "start_date": "2026-09-15T00:00:00+09:00",
                "end_date": "2026-09-15T00:00:01.5+09:00",
                "timezone": "America/New_York",
            },
            "2026-09-14T15:00:00+00:00",
            "2026-09-14T15:00:01.500000+00:00",
        ),
        ({"start_date": "2026-09-15"}, "2026-09-15T00:00:00+00:00", None),
        ({"end_date": "2026-09-15"}, None, "2026-09-16T00:00:00+00:00"),
        ({}, None, None),
    ],
)
async def test_bulk_forwards_normalized_bounds(
    bulk, mocker, operation, calendar_name, dates, start, end
) -> None:
    _, tool, calendar, _ = bulk
    await tool.fn(
        operation=operation,
        ctx=mocker.MagicMock(),
        calendar_name=calendar_name,
        new_location="Updated",
        target_calendar="target",
        **dates,
    )
    query = calendar.search_events_across_calendars
    if calendar_name and operation != "update":
        query = calendar.get_calendar_events
    query.assert_awaited_once()
    assert query.call_args.kwargs["start_datetime"] == (
        dt.datetime.fromisoformat(start) if start else None
    )
    assert query.call_args.kwargs["end_datetime"] == (
        dt.datetime.fromisoformat(end) if end else None
    )
    if operation == "update":
        filters = calendar.bulk_update_events.call_args.args[0]
        assert filters == {
            key: value
            for key, value in (("start_date", start), ("end_date", end))
            if value is not None
        }


@pytest.mark.parametrize("operation", ["update", "delete", "move"])
@pytest.mark.parametrize(
    "dates,message",
    [
        ({"start_date": "not-a-date"}, "start_date"),
        ({"end_date": "2026-02-29"}, "end_date"),
        ({"timezone": "Asia/Seoull"}, "timezone"),
        ({"start_date": "2026-09-16", "end_date": "2026-09-15"}, "start_date"),
        (
            {"start_date": "2026-09-15T00:00:00Z", "end_date": "2026-09-15T00:00:00Z"},
            "start_date",
        ),
    ],
)
async def test_bulk_bad_dates_fail_before_client_access(
    bulk, mocker, operation, dates, message
) -> None:
    _, tool, _, get_client = bulk
    with pytest.raises(ToolError, match=message):
        await tool.fn(operation=operation, ctx=mocker.MagicMock(), **dates)
    get_client.assert_not_called()


async def test_same_day_update_reaches_client_mutation_through_mcp(bulk) -> None:
    mcp, _, calendar, _ = bulk
    calendar.search_events_across_calendars.return_value = [
        {"calendar_name": "personal", "uid": "early", "title": "Early"}
    ]
    async with Client(mcp) as client:
        response = await client.call_tool(
            "nc_calendar_bulk_operations",
            {
                "operation": "update",
                "start_date": "2026-09-15",
                "end_date": "2026-09-15",
                "timezone": "Asia/Seoul",
                "new_location": "Updated",
            },
        )
    assert response.is_error is False
    assert json.loads(response.content[0].text)["updated_count"] == 1
    calendar.update_event.assert_awaited_once_with(
        "personal", "early", {"location": "Updated"}
    )


async def test_bulk_schema_exposes_optional_timezone(bulk) -> None:
    mcp, _, _, _ = bulk
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "nc_calendar_bulk_operations")
    assert tool.input_schema["properties"]["timezone"]["default"] == "UTC"
    assert "timezone" not in tool.input_schema.get("required", [])


@pytest.mark.parametrize("operation", ["update", "delete", "move"])
@pytest.mark.parametrize("apply_to_series", [False, True])
async def test_local_day_bulk_preserves_series_opt_in(
    bulk: tuple[MCPServer, Any, Any, Any], operation: str, apply_to_series: bool
) -> None:
    """A local-day match still requires consent to mutate a whole series."""
    mcp, _, calendar, _ = bulk
    occurrence = {
        "calendar_name": "personal",
        "uid": "series",
        "title": "Recurring",
        "recurring": True,
        "start_datetime": "2026-09-15T03:05:00+09:00",
    }
    calendar.search_events_across_calendars.return_value = [
        occurrence,
        {**occurrence, "start_datetime": "2026-09-15T04:05:00+09:00"},
    ]
    async with Client(mcp) as client:
        response = await client.call_tool(
            "nc_calendar_bulk_operations",
            {
                "operation": operation,
                "start_date": "2026-09-15",
                "end_date": "2026-09-15",
                "timezone": "Asia/Seoul",
                "apply_to_series": apply_to_series,
                "new_location": "Updated",
                "target_calendar": "target",
            },
        )
    assert response.is_error is False
    data = json.loads(response.content[0].text)
    assert data["total_found"] == 1
    skipped = operation == "move" or not apply_to_series
    assert data["skipped_count"] == int(skipped)
    if skipped:
        assert data["results"][0]["status"] == "skipped"
        calendar.update_event.assert_not_awaited()
        calendar.delete_event.assert_not_awaited()
    elif operation == "update":
        calendar.update_event.assert_awaited_once_with(
            "personal", "series", {"location": "Updated"}
        )
    else:
        calendar.delete_event.assert_awaited_once_with("personal", "series")
    calendar.create_event.assert_not_awaited()
    query = calendar.search_events_across_calendars.call_args.kwargs
    assert query["start_datetime"] == dt.datetime(2026, 9, 14, 15, tzinfo=dt.UTC)
    assert query["end_datetime"] == dt.datetime(2026, 9, 15, 15, tzinfo=dt.UTC)
