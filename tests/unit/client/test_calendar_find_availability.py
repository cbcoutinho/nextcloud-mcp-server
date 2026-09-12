"""``CalendarClient.find_availability`` wiring: events in, free slots out.

The interval arithmetic itself is covered in ``test_availability.py``; what is
pinned here is everything the client adds around it -- the search window it
asks for, the attendee free/busy round trip, and the refusals that must not
degrade into an empty (and therefore falsely reassuring) slot list.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from nextcloud_mcp_server.client.calendar import CalendarClient

pytestmark = pytest.mark.unit

TZ = "Europe/Amsterdam"
ZONE = ZoneInfo(TZ)

# A Monday, far enough out that "never suggest a slot in the past" never bites.
MONDAY = (dt.datetime.now(ZONE) + dt.timedelta(days=30)).date()
while MONDAY.weekday() != 0:
    MONDAY += dt.timedelta(days=1)
TUESDAY = MONDAY + dt.timedelta(days=1)


def _client(mocker, events=None, principal=None):
    client = CalendarClient.__new__(CalendarClient)
    client._dav_client = _FakeDav(principal)
    mocker.patch.object(
        CalendarClient,
        "search_events_across_calendars",
        mocker.AsyncMock(return_value=list(events or [])),
    )
    return client


class _FakeReply:
    def __init__(self, data):
        self.data = data


class _FakePrincipal:
    """Stands in for caldav's Principal; freebusy_request is sync here.

    ``_maybe_await`` awaits only what is awaitable, so a plain return value
    exercises the same code path as caldav's coroutine.
    """

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def freebusy_request(self, start, end, attendees):
        self.calls.append((start, end, attendees))
        return self.replies


class _FakeDav:
    def __init__(self, principal):
        self._principal = principal

    def get_principal(self):
        return self._principal


def _local(day, time_text):
    """An aware datetime in ZONE -- never a hardcoded offset.

    MONDAY moves with the clock, so a literal "+02:00" silently becomes an
    hour wrong the first time the rolling window lands after the DST switch.
    """
    return dt.datetime.combine(day, dt.time.fromisoformat(time_text), tzinfo=ZONE)


def _event(day, start, end, **extra):
    return {
        "start_datetime": _local(day, start).isoformat(),
        "end_datetime": _local(day, end).isoformat(),
        **extra,
    }


def _window(days=1):
    start = dt.datetime.combine(MONDAY, dt.time(0, 0), tzinfo=ZONE)
    return start, start + dt.timedelta(days=days)


async def test_empty_calendar_offers_the_whole_business_day(mocker):
    client = _client(mocker)
    start, end = _window()

    result = await client.find_availability(
        60, start_datetime=start, end_datetime=end, constraints={"timezone": TZ}
    )

    assert [(slot["start"], slot["end"]) for slot in result["slots"]] == [
        (
            _local(MONDAY, "09:00").isoformat(),
            _local(MONDAY, "17:00").isoformat(),
        )
    ]
    assert result["attendees_checked"] == []


async def test_a_meeting_splits_the_day(mocker):
    client = _client(mocker, [_event(MONDAY, "11:00", "12:00")])
    start, end = _window()

    result = await client.find_availability(
        60, start_datetime=start, end_datetime=end, constraints={"timezone": TZ}
    )

    assert [slot["duration_minutes"] for slot in result["slots"]] == [120, 300]


async def test_free_and_all_day_events_do_not_consume_time(mocker):
    """The three exclusions, through the real client path (issue #1394)."""
    client = _client(
        mocker,
        [
            _event(MONDAY, "09:00", "17:00", transp="TRANSPARENT"),
            _event(MONDAY, "09:00", "17:00", calendar_transparent=True),
            _event(MONDAY, "09:00", "17:00", status="CANCELLED"),
            {
                "start_datetime": MONDAY.isoformat(),
                "end_datetime": TUESDAY.isoformat(),
                "all_day": True,
            },
        ],
    )
    start, end = _window()

    result = await client.find_availability(
        60, start_datetime=start, end_datetime=end, constraints={"timezone": TZ}
    )

    assert len(result["slots"]) == 1
    assert result["slots"][0]["duration_minutes"] == 480


async def test_the_search_window_is_reported_back(mocker):
    client = _client(mocker)
    start, end = _window(days=2)

    result = await client.find_availability(
        30, start_datetime=start, end_datetime=end, constraints={"timezone": TZ}
    )

    assert result["range_start"] == start.isoformat()
    assert result["range_end"] == end.isoformat()


async def test_search_is_scoped_to_the_requested_window(mocker):
    client = _client(mocker)
    start, end = _window()

    await client.find_availability(
        30, start_datetime=start, end_datetime=end, constraints={"timezone": TZ}
    )

    CalendarClient.search_events_across_calendars.assert_awaited_once_with(
        start_datetime=start, end_datetime=end, limit=mocker.ANY, strict=True
    )


async def test_every_event_in_the_window_is_fetched(mocker):
    """The default per-calendar listing cap would drop busy time silently."""
    from nextcloud_mcp_server.client.calendar import AVAILABILITY_EVENT_LIMIT

    client = _client(mocker)
    start, end = _window()

    await client.find_availability(
        30, start_datetime=start, end_datetime=end, constraints={"timezone": TZ}
    )

    assert (
        CalendarClient.search_events_across_calendars.await_args.kwargs["limit"]
        == AVAILABILITY_EVENT_LIMIT
    )


async def test_the_past_is_never_searched(mocker):
    """A range starting last week would otherwise offer slots already gone."""
    client = _client(mocker)
    now = dt.datetime.now(ZONE)

    result = await client.find_availability(
        30,
        start_datetime=now - dt.timedelta(days=7),
        end_datetime=now + dt.timedelta(days=1),
        constraints={"timezone": TZ},
    )

    assert dt.datetime.fromisoformat(result["range_start"]) >= now


class TestAttendees:
    ICS = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VFREEBUSY\r\nUID:1\r\n"
        "FREEBUSY;FBTYPE=BUSY:{start}/{end}\r\n"
        "END:VFREEBUSY\r\nEND:VCALENDAR\r\n"
    )

    @staticmethod
    def _utc(day, time_text):
        return _local(day, time_text).astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")

    def _busy_all_day_reply(self):
        """Bob is busy for the whole local business day, expressed in UTC."""
        return self.ICS.format(
            start=self._utc(MONDAY, "09:00"), end=self._utc(MONDAY, "17:00")
        )

    async def test_attendee_busy_time_removes_slots(self, mocker):
        principal = _FakePrincipal(
            {"mailto:bob@example.org": _FakeReply(self._busy_all_day_reply())}
        )
        client = _client(mocker, principal=principal)
        start, end = _window()

        result = await client.find_availability(
            60,
            attendees=["bob@example.org"],
            start_datetime=start,
            end_datetime=end,
            constraints={"timezone": TZ},
        )

        # Bob is busy 09:00-17:00 local; nothing is left of the business day.
        assert result["slots"] == []
        assert result["attendees_checked"] == ["bob@example.org"]
        assert principal.calls[0][2] == ["mailto:bob@example.org"]

    async def test_addresses_already_carrying_mailto_are_not_doubled(self, mocker):
        principal = _FakePrincipal(
            {"mailto:bob@example.org": _FakeReply(self._busy_all_day_reply())}
        )
        client = _client(mocker, principal=principal)
        start, end = _window()

        await client.find_availability(
            60,
            attendees=["MAILTO:bob@example.org"],
            start_datetime=start,
            end_datetime=end,
            constraints={"timezone": TZ},
        )

        assert principal.calls[0][2] == ["MAILTO:bob@example.org"]

    async def test_an_attendee_the_server_refuses_raises(self, mocker):
        principal = _FakePrincipal(
            {"errors": {"mailto:bob@example.org": "3.7;Invalid calendar user"}}
        )
        client = _client(mocker, principal=principal)
        start, end = _window()

        with pytest.raises(ValueError, match="bob@example.org"):
            await client.find_availability(
                60,
                attendees=["bob@example.org"],
                start_datetime=start,
                end_datetime=end,
                constraints={"timezone": TZ},
            )

    async def test_a_missing_reply_raises_rather_than_reading_as_free(self, mocker):
        """ "I could not check Bob" must never be reported as "Bob is free"."""
        principal = _FakePrincipal({})
        client = _client(mocker, principal=principal)
        start, end = _window()

        with pytest.raises(ValueError, match="no free/busy information"):
            await client.find_availability(
                60,
                attendees=["bob@example.org"],
                start_datetime=start,
                end_datetime=end,
                constraints={"timezone": TZ},
            )

    async def test_no_attendees_means_no_scheduling_request(self, mocker):
        principal = _FakePrincipal({})
        client = _client(mocker, principal=principal)
        start, end = _window()

        await client.find_availability(
            60,
            attendees=["  "],
            start_datetime=start,
            end_datetime=end,
            constraints={"timezone": TZ},
        )

        assert principal.calls == []


class TestRefusals:
    @pytest.mark.parametrize("duration", [0, -30])
    async def test_non_positive_duration_raises(self, mocker, duration):
        client = _client(mocker)
        start, end = _window()

        with pytest.raises(ValueError, match="duration_minutes"):
            await client.find_availability(
                duration, start_datetime=start, end_datetime=end
            )

    async def test_an_unreadable_calendar_raises(self, mocker):
        """Skipping it would offer its booked hours as free (review round 1)."""
        client = _client(mocker)
        CalendarClient.search_events_across_calendars.side_effect = ValueError(
            "Could not read 1 calendar(s): work (calendar is on fire)"
        )
        start, end = _window()

        with pytest.raises(ValueError, match="Could not read"):
            await client.find_availability(
                60, start_datetime=start, end_datetime=end, constraints={"timezone": TZ}
            )

    async def test_an_unknown_timezone_raises(self, mocker):
        """Falling back to the server's zone would answer the wrong question."""
        client = _client(mocker)
        start, end = _window()

        with pytest.raises(ValueError, match="America/New_Yrok"):
            await client.find_availability(
                60,
                start_datetime=start,
                end_datetime=end,
                constraints={"timezone": "America/New_Yrok"},
            )

    async def test_no_timezone_falls_back_to_the_local_offset(self, mocker):
        """A naive window is read in the server's zone when none is given."""
        client = _client(mocker)
        naive_start = dt.datetime.combine(MONDAY, dt.time(0, 0))

        result = await client.find_availability(
            60,
            start_datetime=naive_start,
            end_datetime=naive_start + dt.timedelta(days=1),
        )

        local = dt.datetime.now().astimezone().tzinfo
        assert dt.datetime.fromisoformat(result["range_start"]).utcoffset() == (
            naive_start.replace(tzinfo=local).utcoffset()
        )

    async def test_inverted_range_raises(self, mocker):
        client = _client(mocker)
        start, end = _window()

        with pytest.raises(ValueError, match="date_range_end"):
            await client.find_availability(60, start_datetime=end, end_datetime=start)
