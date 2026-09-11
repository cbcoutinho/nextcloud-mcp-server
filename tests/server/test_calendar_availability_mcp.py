"""End-to-end availability against a live Nextcloud (issue #1394).

The tool used to return an empty list while reporting success, so the thing
worth proving against a real server is not that it responds -- it always did --
but that real events on a real calendar move the answer.

The window is pushed weeks out on purpose: availability spans *every* calendar
the test user has, so a near-term range would pick up whatever other tests left
behind.
"""

import json
import logging
from datetime import date, datetime, timedelta

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


def _quiet_monday() -> date:
    """A Monday far enough out that no other test has booked anything on it."""
    day = datetime.now().date() + timedelta(days=45)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


async def _call(mcp_client: ClientSession, **arguments) -> dict:
    result = await mcp_client.call_tool("nc_calendar_find_availability", arguments)
    assert result.is_error is False, f"find_availability failed: {result.content}"
    return json.loads(result.content[0].text)


async def test_find_availability_reflects_real_events(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """A booked hour splits the business day; an all-day event does not erase it."""
    day = _quiet_monday()
    created: list[str] = []

    try:
        meeting = await nc_client.calendar.create_event(
            temporary_calendar,
            {
                "title": "Availability probe meeting",
                "start_datetime": f"{day.isoformat()}T11:00:00",
                "end_datetime": f"{day.isoformat()}T12:00:00",
            },
        )
        created.append(meeting["uid"])

        birthday = await nc_client.calendar.create_event(
            temporary_calendar,
            {
                "title": "Availability probe birthday",
                "start_datetime": day.isoformat(),
                "end_datetime": (day + timedelta(days=1)).isoformat(),
                "all_day": True,
            },
        )
        created.append(birthday["uid"])

        data = await _call(
            nc_mcp_client,
            duration_minutes=60,
            date_range_start=day.isoformat(),
            date_range_end=day.isoformat(),
        )

        assert data["success"] is True
        assert data["duration_requested"] == 60
        assert data["attendees_checked"] == []

        slots = data["available_slots"]
        # The all-day event must not have blanked the day out.
        assert slots, "expected free time on an otherwise empty Monday"

        by_start = {slot["start"][11:19]: slot for slot in slots}
        assert "09:00:00" in by_start, f"morning slot missing from {slots}"
        assert by_start["09:00:00"]["end"][11:19] == "11:00:00"
        assert by_start["09:00:00"]["duration_minutes"] == 120
        assert "12:00:00" in by_start, f"afternoon slot missing from {slots}"
        assert by_start["12:00:00"]["end"][11:19] == "17:00:00"
        assert all(slot["date"] == day.isoformat() for slot in slots)

        # Opting in to all-day events books the whole day out.
        with_all_day = await _call(
            nc_mcp_client,
            duration_minutes=60,
            date_range_start=day.isoformat(),
            date_range_end=day.isoformat(),
            include_all_day=True,
        )
        assert with_all_day["available_slots"] == []

    finally:
        for uid in created:
            try:
                await nc_client.calendar.delete_event(temporary_calendar, uid)
            except Exception as e:
                logger.warning("Error deleting probe event %s: %s", uid, e)


async def test_find_availability_honours_the_search_constraints(
    nc_mcp_client: ClientSession,
):
    """Preferred times replace business hours; the window comes back as searched."""
    day = _quiet_monday()

    data = await _call(
        nc_mcp_client,
        duration_minutes=30,
        date_range_start=day.isoformat(),
        date_range_end=day.isoformat(),
        preferred_times="19:00-21:00",
    )

    assert [
        (slot["start"][11:19], slot["end"][11:19]) for slot in data["available_slots"]
    ] == [("19:00:00", "21:00:00")]
    assert data["date_range_start"].startswith(day.isoformat())
    assert data["date_range_end"].startswith(day.isoformat())


async def test_find_availability_rejects_an_impossible_request(
    nc_mcp_client: ClientSession,
):
    """A refusal must surface as an error, not as "no slots" (issue #1394)."""
    result = await nc_mcp_client.call_tool(
        "nc_calendar_find_availability", {"duration_minutes": 0}
    )

    assert result.is_error is True
    assert "duration_minutes" in result.content[0].text
