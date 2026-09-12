"""Availability is interval arithmetic, so it is tested as interval arithmetic.

``find_availability`` used to be a stub returning ``[]`` while reporting success
(issue #1394), which is the failure these tests exist to keep from coming back:
every case below asserts on *which* slots come out, never merely that something
did.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from nextcloud_mcp_server.client.availability import (
    busy_spans_from_events,
    busy_spans_from_vfreebusy,
    daily_windows,
    free_slots,
    merge_spans,
    parse_time_ranges,
    slot_to_dict,
)

pytestmark = pytest.mark.unit

TZ = ZoneInfo("Europe/Amsterdam")


def at(day: str, time: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=TZ)


class TestParseTimeRanges:
    def test_parses_comma_separated_string(self):
        assert parse_time_ranges("09:00-12:00,14:00-17:30") == [
            (dt.time(9), dt.time(12)),
            (dt.time(14), dt.time(17, 30)),
        ]

    def test_accepts_an_already_split_list(self):
        assert parse_time_ranges([" 09:00-12:00 "]) == [(dt.time(9), dt.time(12))]

    def test_empty_input_is_no_constraint(self):
        assert parse_time_ranges("") == []
        assert parse_time_ranges(None) == []

    @pytest.mark.parametrize("bad", ["09:00", "not-a-range", "25:00-26:00", "-"])
    def test_malformed_ranges_are_skipped_not_raised(self, bad):
        assert parse_time_ranges(f"{bad},09:00-10:00") == [(dt.time(9), dt.time(10))]

    def test_inverted_range_is_skipped(self):
        assert parse_time_ranges("17:00-09:00") == []


class TestBusySpansFromEvents:
    TIMED = {
        "start_datetime": "2026-09-14T10:00:00+02:00",
        "end_datetime": "2026-09-14T11:00:00+02:00",
    }

    def test_plain_event_is_busy(self):
        assert busy_spans_from_events([self.TIMED], tz=TZ) == [
            (at("2026-09-14", "10:00"), at("2026-09-14", "11:00"))
        ]

    def test_transparent_event_does_not_block(self):
        assert (
            busy_spans_from_events([{**self.TIMED, "transp": "TRANSPARENT"}], tz=TZ)
            == []
        )

    def test_event_on_a_transparent_calendar_does_not_block(self):
        events = [{**self.TIMED, "calendar_transparent": True}]
        assert busy_spans_from_events(events, tz=TZ) == []

    def test_cancelled_event_does_not_block(self):
        assert (
            busy_spans_from_events([{**self.TIMED, "status": "CANCELLED"}], tz=TZ) == []
        )

    def test_all_day_event_is_ignored_by_default(self):
        """Birthdays and holiday feeds must not blank out the working day."""
        birthday = {
            "start_datetime": "2026-09-14",
            "end_datetime": "2026-09-15",
            "all_day": True,
        }
        assert busy_spans_from_events([birthday], tz=TZ) == []

    def test_all_day_event_blocks_when_opted_in(self):
        vacation = {
            "start_datetime": "2026-09-14",
            "end_datetime": "2026-09-15",
            "all_day": True,
        }
        assert busy_spans_from_events([vacation], tz=TZ, include_all_day=True) == [
            (at("2026-09-14", "00:00"), at("2026-09-15", "00:00"))
        ]

    def test_all_day_event_without_end_covers_its_day(self):
        event = {"start_datetime": "2026-09-14", "all_day": True}
        assert busy_spans_from_events([event], tz=TZ, include_all_day=True) == [
            (at("2026-09-14", "00:00"), at("2026-09-15", "00:00"))
        ]

    def test_floating_times_are_read_in_the_requested_zone(self):
        event = {
            "start_datetime": "2026-09-14T10:00:00",
            "end_datetime": "2026-09-14T11:00:00",
        }
        assert busy_spans_from_events([event], tz=TZ) == [
            (at("2026-09-14", "10:00"), at("2026-09-14", "11:00"))
        ]

    @pytest.mark.parametrize(
        "event",
        [
            {"end_datetime": "2026-09-14T11:00:00+02:00"},
            {"start_datetime": "2026-09-14T11:00:00+02:00"},
            {
                "start_datetime": "2026-09-14T11:00:00+02:00",
                "end_datetime": "2026-09-14T10:00:00+02:00",
            },
            {"start_datetime": "nonsense", "end_datetime": "also nonsense"},
        ],
        ids=["no-start", "zero-length", "inverted", "unparseable"],
    )
    def test_degenerate_events_are_skipped(self, event):
        assert busy_spans_from_events([event], tz=TZ) == []


class TestBusySpansFromVFreeBusy:
    ICS = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VFREEBUSY\r\nUID:1\r\n"
        "DTSTART:20260914T000000Z\r\nDTEND:20260915T000000Z\r\n"
        "{body}"
        "END:VFREEBUSY\r\nEND:VCALENDAR\r\n"
    )

    def test_busy_periods_are_read(self):
        spans = busy_spans_from_vfreebusy(
            self.ICS.format(
                body="FREEBUSY;FBTYPE=BUSY:20260914T080000Z/20260914T090000Z\r\n"
            ),
            TZ,
        )
        assert spans == [(at("2026-09-14", "10:00"), at("2026-09-14", "11:00"))]

    def test_period_given_as_a_duration_is_expanded(self):
        spans = busy_spans_from_vfreebusy(
            self.ICS.format(body="FREEBUSY:20260914T080000Z/PT90M\r\n"), TZ
        )
        assert spans == [(at("2026-09-14", "10:00"), at("2026-09-14", "11:30"))]

    def test_free_periods_do_not_block(self):
        spans = busy_spans_from_vfreebusy(
            self.ICS.format(
                body="FREEBUSY;FBTYPE=FREE:20260914T080000Z/20260914T090000Z\r\n"
            ),
            TZ,
        )
        assert spans == []

    def test_tentative_and_unavailable_still_block(self):
        spans = busy_spans_from_vfreebusy(
            self.ICS.format(
                body=(
                    "FREEBUSY;FBTYPE=BUSY-TENTATIVE:20260914T080000Z/20260914T090000Z\r\n"
                    "FREEBUSY;FBTYPE=BUSY-UNAVAILABLE:20260914T100000Z/20260914T110000Z\r\n"
                )
            ),
            TZ,
        )
        assert len(spans) == 2

    def test_reply_without_periods_is_empty_not_an_error(self):
        assert busy_spans_from_vfreebusy(self.ICS.format(body=""), TZ) == []

    def test_unparseable_reply_is_logged_and_ignored(self):
        assert busy_spans_from_vfreebusy("not an icalendar object", TZ) == []


class TestMergeSpans:
    def test_overlapping_spans_collapse(self):
        spans = [
            (at("2026-09-14", "10:00"), at("2026-09-14", "11:00")),
            (at("2026-09-14", "10:30"), at("2026-09-14", "12:00")),
        ]
        assert merge_spans(spans) == [
            (at("2026-09-14", "10:00"), at("2026-09-14", "12:00"))
        ]

    def test_touching_spans_collapse(self):
        spans = [
            (at("2026-09-14", "10:00"), at("2026-09-14", "11:00")),
            (at("2026-09-14", "11:00"), at("2026-09-14", "12:00")),
        ]
        assert merge_spans(spans) == [
            (at("2026-09-14", "10:00"), at("2026-09-14", "12:00"))
        ]

    def test_a_span_swallowed_by_an_earlier_one_does_not_shorten_it(self):
        spans = [
            (at("2026-09-14", "09:00"), at("2026-09-14", "17:00")),
            (at("2026-09-14", "10:00"), at("2026-09-14", "11:00")),
        ]
        assert merge_spans(spans) == [
            (at("2026-09-14", "09:00"), at("2026-09-14", "17:00"))
        ]

    def test_disjoint_spans_are_returned_in_order(self):
        later = (at("2026-09-14", "15:00"), at("2026-09-14", "16:00"))
        earlier = (at("2026-09-14", "10:00"), at("2026-09-14", "11:00"))
        assert merge_spans([later, earlier]) == [earlier, later]


class TestDailyWindows:
    # Monday 2026-09-14 .. Wednesday 2026-09-16
    START = at("2026-09-14", "00:00")
    END = at("2026-09-17", "00:00")

    def test_business_hours_per_weekday(self):
        windows = daily_windows(self.START, self.END, tz=TZ)
        assert windows == [
            (at("2026-09-14", "09:00"), at("2026-09-14", "17:00")),
            (at("2026-09-15", "09:00"), at("2026-09-15", "17:00")),
            (at("2026-09-16", "09:00"), at("2026-09-16", "17:00")),
        ]

    def test_weekends_are_skipped(self):
        # Friday .. Monday
        windows = daily_windows(
            at("2026-09-18", "00:00"), at("2026-09-22", "00:00"), tz=TZ
        )
        assert [window[0].date().isoformat() for window in windows] == [
            "2026-09-18",
            "2026-09-21",
        ]

    def test_weekends_are_kept_when_asked(self):
        windows = daily_windows(
            at("2026-09-18", "00:00"),
            at("2026-09-22", "00:00"),
            tz=TZ,
            exclude_weekends=False,
        )
        assert len(windows) == 4

    def test_whole_days_without_business_hours(self):
        windows = daily_windows(
            self.START, at("2026-09-15", "00:00"), tz=TZ, business_hours_only=False
        )
        assert windows == [(at("2026-09-14", "00:00"), at("2026-09-15", "00:00"))]

    def test_preferred_times_replace_business_hours(self):
        """Not intersect: 08:00-09:00 under the default would otherwise vanish."""
        windows = daily_windows(
            self.START,
            at("2026-09-15", "00:00"),
            tz=TZ,
            preferred_times=[(dt.time(8), dt.time(9))],
        )
        assert windows == [(at("2026-09-14", "08:00"), at("2026-09-14", "09:00"))]

    def test_overlapping_preferred_ranges_do_not_duplicate_a_window(self):
        windows = daily_windows(
            self.START,
            at("2026-09-15", "00:00"),
            tz=TZ,
            preferred_times=[(dt.time(9), dt.time(12)), (dt.time(11), dt.time(14))],
        )
        assert windows == [(at("2026-09-14", "09:00"), at("2026-09-14", "14:00"))]

    def test_windows_are_clipped_to_the_search_range(self):
        windows = daily_windows(
            at("2026-09-14", "10:30"), at("2026-09-14", "12:00"), tz=TZ
        )
        assert windows == [(at("2026-09-14", "10:30"), at("2026-09-14", "12:00"))]

    def test_range_entirely_outside_business_hours_yields_nothing(self):
        assert (
            daily_windows(at("2026-09-14", "18:00"), at("2026-09-14", "20:00"), tz=TZ)
            == []
        )


class TestFreeSlots:
    WINDOW = [(at("2026-09-14", "09:00"), at("2026-09-14", "17:00"))]
    HOUR = dt.timedelta(hours=1)

    def test_no_busy_time_leaves_the_whole_window(self):
        assert free_slots(self.WINDOW, [], self.HOUR) == self.WINDOW

    def test_a_meeting_splits_the_window(self):
        busy = [(at("2026-09-14", "11:00"), at("2026-09-14", "12:00"))]
        assert free_slots(self.WINDOW, busy, self.HOUR) == [
            (at("2026-09-14", "09:00"), at("2026-09-14", "11:00")),
            (at("2026-09-14", "12:00"), at("2026-09-14", "17:00")),
        ]

    def test_gaps_shorter_than_the_duration_are_dropped(self):
        busy = [
            (at("2026-09-14", "09:30"), at("2026-09-14", "12:00")),
            (at("2026-09-14", "12:30"), at("2026-09-14", "17:00")),
        ]
        assert free_slots(self.WINDOW, busy, self.HOUR) == []

    def test_overlapping_meetings_do_not_produce_phantom_gaps(self):
        busy = [
            (at("2026-09-14", "10:00"), at("2026-09-14", "13:00")),
            (at("2026-09-14", "11:00"), at("2026-09-14", "12:00")),
        ]
        assert free_slots(self.WINDOW, busy, self.HOUR) == [
            (at("2026-09-14", "09:00"), at("2026-09-14", "10:00")),
            (at("2026-09-14", "13:00"), at("2026-09-14", "17:00")),
        ]

    def test_busy_time_outside_the_window_is_irrelevant(self):
        busy = [(at("2026-09-14", "06:00"), at("2026-09-14", "08:00"))]
        assert free_slots(self.WINDOW, busy, self.HOUR) == self.WINDOW

    def test_a_fully_booked_day_yields_nothing(self):
        busy = [(at("2026-09-14", "08:00"), at("2026-09-14", "18:00"))]
        assert free_slots(self.WINDOW, busy, self.HOUR) == []

    def test_slots_are_found_across_several_windows(self):
        windows = [
            (at("2026-09-14", "09:00"), at("2026-09-14", "17:00")),
            (at("2026-09-15", "09:00"), at("2026-09-15", "17:00")),
        ]
        busy = [(at("2026-09-14", "09:00"), at("2026-09-14", "17:00"))]
        assert free_slots(windows, busy, self.HOUR) == [windows[1]]


class TestSlotToDict:
    def test_shape_matches_the_response_model(self):
        slot = slot_to_dict((at("2026-09-14", "09:00"), at("2026-09-14", "10:30")))
        assert slot == {
            "start": "2026-09-14T09:00:00+02:00",
            "end": "2026-09-14T10:30:00+02:00",
            "duration_minutes": 90,
            "date": "2026-09-14",
        }
