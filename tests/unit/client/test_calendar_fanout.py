"""Unit tests for concurrent cross-calendar search.

``search_events_across_calendars`` and ``search_todos_across_calendars`` used
to ``await`` one calendar after another, so a REPORT round trip was paid per
calendar and the wall clock grew linearly with how many calendars an account
has -- almost independently of how many events came back.

These pin the three properties that matter and are easy to lose when a serial
loop becomes a task group: the calls really do overlap, one broken calendar
does not take the rest down with it, and the result order still follows the
calendar order rather than whichever REPORT happened to finish first.
"""

import anyio
import pytest

from nextcloud_mcp_server.client.calendar import CALENDAR_FANOUT, CalendarClient

pytestmark = pytest.mark.unit

CALENDARS = [{"name": f"cal{i}", "display_name": f"Cal {i}"} for i in range(6)]


@pytest.fixture
def client(mocker):
    """CalendarClient with its DAV client and calendar home mocked out."""
    mocker.patch("nextcloud_mcp_server.client.calendar.AsyncDAVClient")
    client = CalendarClient("https://cloud.example.org", "alice", password="pw")
    mocker.patch.object(client, "_ensure_calendar_home", return_value=None)
    mocker.patch.object(client, "list_calendars", return_value=list(CALENDARS))
    return client


class _Tracker:
    """Records how many calls are in flight at the same time."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def run(self, name: str, *args, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        # Yield so the other tasks get a chance to start before this returns;
        # a serial loop would never let `active` climb above 1.
        await anyio.sleep(0.01)
        self.active -= 1
        return [{"uid": f"{name}-1"}]


async def test_events_are_fetched_concurrently(client, mocker):
    tracker = _Tracker()
    mocker.patch.object(client, "get_calendar_events", side_effect=tracker.run)

    events = await client.search_events_across_calendars()

    assert len(events) == len(CALENDARS)
    assert tracker.peak > 1, "calendars were still queried one after another"
    assert tracker.peak <= CALENDAR_FANOUT


async def test_events_keep_calendar_order(client, mocker):
    async def events_for(name, *args, **kwargs):
        # Later calendars answer first, so appending in completion order
        # would visibly scramble the result.
        await anyio.sleep(0.05 / (int(name[-1]) + 1))
        return [{"uid": f"{name}-1"}]

    mocker.patch.object(client, "get_calendar_events", side_effect=events_for)

    events = await client.search_events_across_calendars()

    assert [e["calendar_name"] for e in events] == [c["name"] for c in CALENDARS]
    assert events[0]["calendar_display_name"] == "Cal 0"


async def test_one_broken_calendar_does_not_sink_the_rest(client, mocker):
    async def events_for(name, *args, **kwargs):
        if name == "cal2":
            raise RuntimeError("calendar is on fire")
        return [{"uid": f"{name}-1"}]

    mocker.patch.object(client, "get_calendar_events", side_effect=events_for)

    events = await client.search_events_across_calendars()

    names = [e["calendar_name"] for e in events]
    assert "cal2" not in names
    assert len(names) == len(CALENDARS) - 1


async def test_filters_are_applied_per_calendar(client, mocker):
    mocker.patch.object(client, "get_calendar_events", side_effect=_one)
    applied = mocker.patch.object(
        client, "_apply_event_filters", side_effect=lambda events, f: events
    )

    await client.search_events_across_calendars(filters={"status": "CONFIRMED"})

    assert applied.call_count == len(CALENDARS)


async def test_fanout_is_bounded(client, mocker):
    many = [{"name": f"cal{i}", "display_name": f"Cal {i}"} for i in range(40)]
    mocker.patch.object(client, "list_calendars", return_value=many)
    tracker = _Tracker()
    mocker.patch.object(client, "get_calendar_events", side_effect=tracker.run)

    await client.search_events_across_calendars()

    # Both bounds matter: 1 would mean the loop went serial again, 40 would
    # mean one open connection per calendar with no limiter in between.
    assert 1 < tracker.peak <= CALENDAR_FANOUT


async def test_todos_are_fetched_concurrently(client, mocker):
    tracker = _Tracker()
    mocker.patch.object(client, "list_todos", side_effect=tracker.run)

    todos = await client.search_todos_across_calendars()

    assert len(todos) == len(CALENDARS)
    assert tracker.peak > 1, "calendars were still queried one after another"
    assert [t["calendar_name"] for t in todos] == [c["name"] for c in CALENDARS]


async def test_one_broken_calendar_does_not_sink_todos(client, mocker):
    async def todos_for(name, *args, **kwargs):
        if name == "cal4":
            raise RuntimeError("todo list is on fire")
        return [{"uid": f"{name}-1"}]

    mocker.patch.object(client, "list_todos", side_effect=todos_for)

    todos = await client.search_todos_across_calendars()

    assert "cal4" not in [t["calendar_name"] for t in todos]
    assert len(todos) == len(CALENDARS) - 1


async def _one(name: str, *args, **kwargs):
    return [{"uid": f"{name}-1"}]
