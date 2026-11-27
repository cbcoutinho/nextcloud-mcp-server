"""Round-trip properties for the iCalendar the calendar client writes and reads.

``_create_ical_event`` is what create_event stores; ``_parse_ical_event`` is
what every read returns. Whatever a caller writes must read back unchanged,
or a read-modify-write silently rewrites fields the caller never touched.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st
from nextcloud_client.calendar import CalendarClient

pytestmark = pytest.mark.unit

ZONES = ["Europe/Amsterdam", "America/New_York", "Asia/Kolkata"]


def _client() -> CalendarClient:
    # The iCal helpers touch no DAV session, so skip __init__ (and credentials).
    return CalendarClient.__new__(CalendarClient)


#: Free text an event field may hold. A bare CR is left out: RFC 5545 TEXT can
#: escape only a newline, so icalendar reads ``\r`` back as ``\n``.
_text = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cs",), exclude_characters="\r")
)

#: A category name: the write side splits on commas and strips each name.
_category = _text.filter(lambda c: "," not in c and c.strip() == c and c)

_datetimes = st.datetimes(
    min_value=dt.datetime(1971, 1, 1), max_value=dt.datetime(2100, 1, 1)
).map(lambda d: d.replace(microsecond=0))


@st.composite
def event_times(draw):
    """``(fields sent, fields read back)`` for DTSTART/DTEND.

    All-day events store dates; timed events are floating local time, UTC
    (``Z`` reads back as ``+00:00``), or bound to an IANA zone, which reads
    back as the local time with its offset plus the zone name.
    """
    start = draw(_datetimes)
    end = start + dt.timedelta(seconds=draw(st.integers(0, 40 * 86400)))
    kind = draw(st.sampled_from(["all_day", "floating", "utc", "zoned"]))
    if kind == "all_day":
        sent = {
            "all_day": True,
            "start_datetime": start.date().isoformat(),
            "end_datetime": end.date().isoformat(),
        }
        return sent, sent
    if kind == "floating":
        sent = {"start_datetime": start.isoformat(), "end_datetime": end.isoformat()}
        return sent, sent | {"all_day": False}
    if kind == "utc":
        sent = {
            "start_datetime": start.isoformat() + "Z",
            "end_datetime": end.isoformat() + "Z",
        }
        return sent, {
            "all_day": False,
            "start_datetime": start.isoformat() + "+00:00",
            "end_datetime": end.isoformat() + "+00:00",
        }
    zone = draw(st.sampled_from(ZONES))
    zi = ZoneInfo(zone)
    sent = {
        "timezone": zone,
        "start_datetime": start.isoformat(),
        "end_datetime": end.isoformat(),
    }
    return sent, {
        "all_day": False,
        "start_datetime": start.replace(tzinfo=zi).isoformat(),
        "end_datetime": end.replace(tzinfo=zi).isoformat(),
        "start_tz": zone,
        "end_tz": zone,
    }


@st.composite
def events(draw):
    """``(event_data as a caller writes it, the fields a read must return)``."""
    sent = {
        "title": draw(_text),
        "description": draw(_text),
        "location": draw(_text),
        "priority": draw(st.integers(0, 9)),
        "status": draw(st.sampled_from(["CONFIRMED", "TENTATIVE", "CANCELLED"])),
        "privacy": draw(st.sampled_from(["PUBLIC", "PRIVATE", "CONFIDENTIAL"])),
    }
    expected = dict(sent)
    times_sent, times_read = draw(event_times())
    sent |= times_sent
    expected |= times_read
    categories = draw(st.lists(_category, max_size=3))
    if categories:
        sent["categories"] = ",".join(categories)
        expected["categories"] = ", ".join(categories)
    url = draw(
        st.none()
        | st.from_regex(r"https://[a-z]{1,10}\.example/[a-z0-9]{0,8}", fullmatch=True)
    )
    if url:
        sent["url"] = expected["url"] = url
    color = draw(st.none() | st.sampled_from(["red", "darkseagreen", "teal"]))
    if color:
        sent["color"] = expected["color"] = color
    return sent, expected


@given(events(), st.uuids().map(str))
def test_event_reads_back_what_was_written(case, uid):
    sent, expected = case
    client = _client()
    parsed = client._parse_ical_event(client._create_ical_event(sent, uid))
    assert parsed is not None
    assert {k: parsed[k] for k in expected} == expected
    assert parsed["uid"] == uid


#: The properties the reader interprets, so a malformed value reaches the code
#: that converts it rather than stopping at the outer parse.
_READ_PROPERTIES = [
    "DTSTART",
    "DTEND",
    "DURATION",
    "PRIORITY",
    "RRULE",
    "RECURRENCE-ID",
    "ATTENDEE",
    "ORGANIZER",
    "CATEGORIES",
    "SUMMARY",
    "COLOR",
    "TRANSP",
]
_line_text = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), max_size=20
)
_property_lines = st.tuples(
    st.sampled_from(_READ_PROPERTIES),
    st.sampled_from(["", ";VALUE=DATE", ";TZID=Europe/Amsterdam", ";TZID=Nowhere/X"]),
    _line_text,
).map(lambda t: f"{t[0]}{t[1]}:{t[2]}")

#: A calendar object whose VEVENT carries arbitrary values for those properties.
_vevents = st.lists(_property_lines, max_size=6).map(
    lambda lines: "\r\n".join(
        ["BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:x", *lines]
        + ["END:VEVENT", "END:VCALENDAR", ""]
    )
)


@given(_vevents | st.text(st.characters(codec="utf-8", exclude_categories=("Cs",))))
@example(
    "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:garbage\r\nEND:VEVENT\r\nEND:VCALENDAR"
)
@example("BEGIN:VCALENDAR\r\nEND:VCALENDAR")
def test_any_ical_text_parses_to_an_event_or_none(text):
    """A stored object is server data; an unreadable one is skipped (None),
    never raised out of a listing."""
    parsed = _client()._parse_ical_event(text)
    assert parsed is None or isinstance(parsed, dict)
    event("parsed an event" if parsed else "skipped")
