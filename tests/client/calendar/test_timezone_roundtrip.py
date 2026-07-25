"""Unit tests for calendar timezone roundtrip (issue #782).

These tests cover the three storage flavors that ``_extract_vevent_data`` and
``_create_ical_event`` must handle correctly:

- **Floating local time** (no ``Z``, no offset, no TZID) — RFC 5545's neutral
  wall-clock format.
- **UTC** (``Z`` or ``+00:00`` suffix).
- **TZID-bound** (``DTSTART;TZID=...:...`` with a paired ``VTIMEZONE``).

Prior to issue #782 the read path silently coerced everything to UTC because
``_search_events_by_date`` requested server-side ``<C:expand>``; these tests
pin the post-fix contract so the regression cannot return.
"""

from __future__ import annotations

import httpx
import pytest

from nextcloud_mcp_server.client.calendar import CalendarClient

pytestmark = pytest.mark.unit


def _make_client(mocker) -> CalendarClient:
    """Build a CalendarClient without performing any network IO.

    The pure iCal helpers under test (``_create_ical_event`` /
    ``_parse_ical_event``) don't touch the wire, so a stub AsyncClient is fine.
    """
    client = CalendarClient.__new__(CalendarClient)
    client._client = mocker.AsyncMock(spec=httpx.AsyncClient)
    client._username = "tester"
    return client


# ============= Read path: _parse_ical_event preserves DTSTART semantics =============


def _wrap_vevent(vevent_body: str, vtimezone: str = "") -> str:
    """Assemble a minimal VCALENDAR around a VEVENT body for parser tests."""
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//EN\r\n"
        f"{vtimezone}"
        "BEGIN:VEVENT\r\n"
        "UID:test-event\r\n"
        "SUMMARY:Test\r\n"
        f"{vevent_body}"
        "DTSTAMP:20260510T000000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def test_parse_floating_event_has_no_offset_and_no_tzid(mocker):
    """Floating-local DTSTART must round-trip as a naive ISO string with no spurious offset."""
    client = _make_client(mocker)
    ical = _wrap_vevent("DTSTART:20260513T143000\r\nDTEND:20260513T154500\r\n")

    parsed = client._parse_ical_event(ical)

    assert parsed is not None
    assert parsed["start_datetime"] == "2026-05-13T14:30:00"
    assert parsed["end_datetime"] == "2026-05-13T15:45:00"
    assert "start_tz" not in parsed
    assert "end_tz" not in parsed
    assert parsed["all_day"] is False


def test_parse_utc_event_keeps_explicit_zero_offset(mocker):
    """``DTSTART:...Z`` must serialize back as ``+00:00`` so callers can recognize UTC."""
    client = _make_client(mocker)
    ical = _wrap_vevent("DTSTART:20260512T143000Z\r\nDTEND:20260512T154500Z\r\n")

    parsed = client._parse_ical_event(ical)

    assert parsed is not None
    assert parsed["start_datetime"] == "2026-05-12T14:30:00+00:00"
    assert parsed["end_datetime"] == "2026-05-12T15:45:00+00:00"
    assert "start_tz" not in parsed


def test_parse_tzid_event_exposes_iana_name_and_offset(mocker):
    """TZID-bound events must expose both the resolved offset and the IANA name."""
    client = _make_client(mocker)
    vtz = (
        "BEGIN:VTIMEZONE\r\n"
        "TZID:America/New_York\r\n"
        "BEGIN:STANDARD\r\n"
        "DTSTART:20071104T020000\r\n"
        "TZOFFSETFROM:-0400\r\n"
        "TZOFFSETTO:-0500\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n"
        "END:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "DTSTART:20070311T020000\r\n"
        "TZOFFSETFROM:-0500\r\n"
        "TZOFFSETTO:-0400\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\n"
        "END:DAYLIGHT\r\n"
        "END:VTIMEZONE\r\n"
    )
    ical = _wrap_vevent(
        "DTSTART;TZID=America/New_York:20260514T100000\r\n"
        "DTEND;TZID=America/New_York:20260514T110000\r\n",
        vtimezone=vtz,
    )

    parsed = client._parse_ical_event(ical)

    assert parsed is not None
    # May is EDT (UTC-4)
    assert parsed["start_datetime"] == "2026-05-14T10:00:00-04:00"
    assert parsed["end_datetime"] == "2026-05-14T11:00:00-04:00"
    assert parsed["start_tz"] == "America/New_York"
    assert parsed["end_tz"] == "America/New_York"


def test_parse_all_day_event(mocker):
    """All-day events serialize as plain dates with all_day=True."""
    client = _make_client(mocker)
    ical = _wrap_vevent("DTSTART;VALUE=DATE:20260601\r\nDTEND;VALUE=DATE:20260602\r\n")

    parsed = client._parse_ical_event(ical)

    assert parsed is not None
    assert parsed["all_day"] is True
    assert parsed["start_datetime"] == "2026-06-01"


# ============= Write path: timezone parameter wires TZID + VTIMEZONE =============


def test_create_ical_event_utc_input_stores_as_z_suffix(mocker):
    """Offset-aware input continues to emit RFC 5545 UTC (``...Z``) on the wire."""
    client = _make_client(mocker)
    event_data = {
        "title": "UTC event",
        "start_datetime": "2026-05-12T14:30:00+00:00",
        "end_datetime": "2026-05-12T15:45:00+00:00",
    }

    ical = client._create_ical_event(event_data, event_uid="utc-uid")

    assert "DTSTART:20260512T143000Z" in ical
    assert "DTEND:20260512T154500Z" in ical
    assert "VTIMEZONE" not in ical


def test_create_ical_event_naive_without_tz_stores_floating(mocker):
    """Naive input + no ``timezone`` parameter must store as floating local time."""
    client = _make_client(mocker)
    event_data = {
        "title": "Floating event",
        "start_datetime": "2026-05-13T14:30:00",
        "end_datetime": "2026-05-13T15:45:00",
    }

    ical = client._create_ical_event(event_data, event_uid="floating-uid")

    # No TZID, no Z suffix — RFC 5545 floating local time.
    assert "DTSTART:20260513T143000" in ical
    assert "DTSTART;TZID" not in ical
    assert "20260513T143000Z" not in ical
    assert "VTIMEZONE" not in ical


def test_create_ical_event_naive_with_timezone_emits_tzid_and_vtimezone(mocker):
    """Naive input + ``timezone="America/New_York"`` produces TZID-bound DTSTART + VTIMEZONE."""
    client = _make_client(mocker)
    event_data = {
        "title": "TZID event",
        "start_datetime": "2026-05-14T10:00:00",
        "end_datetime": "2026-05-14T11:00:00",
        "timezone": "America/New_York",
    }

    ical = client._create_ical_event(event_data, event_uid="tzid-uid")

    assert "DTSTART;TZID=America/New_York:20260514T100000" in ical
    assert "DTEND;TZID=America/New_York:20260514T110000" in ical
    # VTIMEZONE component must be emitted so other CalDAV clients can interpret the TZID.
    assert "BEGIN:VTIMEZONE" in ical
    assert "TZID:America/New_York" in ical


def test_create_ical_event_offset_input_ignores_timezone_param(mocker):
    """When the input already carries an offset, ``timezone`` is ignored (warning logged)."""
    client = _make_client(mocker)
    event_data = {
        "title": "Mixed-signals event",
        "start_datetime": "2026-05-12T14:30:00+00:00",
        "end_datetime": "2026-05-12T15:45:00+00:00",
        "timezone": "America/New_York",
    }

    ical = client._create_ical_event(event_data, event_uid="mixed-uid")

    assert "DTSTART:20260512T143000Z" in ical
    assert "DTSTART;TZID" not in ical
    # No VTIMEZONE since we never attached a ZoneInfo.
    assert "VTIMEZONE" not in ical


def test_create_ical_event_unknown_timezone_falls_back_to_floating(mocker, caplog):
    """An unresolvable IANA name must not crash — fall back to floating local time."""
    client = _make_client(mocker)
    event_data = {
        "title": "Bogus TZ event",
        "start_datetime": "2026-05-15T09:00:00",
        "end_datetime": "2026-05-15T10:00:00",
        "timezone": "Continent/Imaginary",
    }

    ical = client._create_ical_event(event_data, event_uid="bogus-uid")

    assert "DTSTART:20260515T090000" in ical
    assert "VTIMEZONE" not in ical


# ============= End-to-end: write → re-parse roundtrip preserves intent =============


def test_roundtrip_tzid_event_preserves_iana_name(mocker):
    """The TZID name placed on write must survive a re-parse on read."""
    client = _make_client(mocker)
    event_data = {
        "title": "Roundtrip TZID",
        "start_datetime": "2026-05-14T10:00:00",
        "end_datetime": "2026-05-14T11:00:00",
        "timezone": "America/New_York",
    }

    ical = client._create_ical_event(event_data, event_uid="roundtrip-uid")
    parsed = client._parse_ical_event(ical)

    assert parsed is not None
    assert parsed["start_tz"] == "America/New_York"
    assert parsed["start_datetime"] == "2026-05-14T10:00:00-04:00"


# ============= Update path: _merge_ical_properties preserves stored shape =============
#
# The update path had no unit coverage at all, which is why the value-type and
# TZID bugs below survived. Assertions are on the parsed value type and params,
# not on rendered strings.


def _merge(mocker, vevent_body: str, event_data: dict, vtimezone: str = ""):
    """Run _merge_ical_properties over a stored VEVENT and parse the result back."""
    from icalendar import Calendar

    client = _make_client(mocker)
    merged = client._merge_ical_properties(
        _wrap_vevent(vevent_body, vtimezone), event_data, "test-event"
    )
    cal = Calendar.from_ical(merged)
    return next(c for c in cal.walk() if c.name == "VEVENT")


def _is_date_only(value) -> bool:
    import datetime as _dt

    return isinstance(value.dt, _dt.date) and not isinstance(value.dt, _dt.datetime)


def test_all_day_event_stays_all_day_when_all_day_not_repassed(mocker):
    """Headline regression: updating an all-day event's start without re-passing
    ``all_day=True`` used to rewrite DTSTART as naive floating DATE-TIME."""
    vevent = "DTSTART;VALUE=DATE:20260101\r\nDTEND;VALUE=DATE:20260102\r\n"
    component = _merge(mocker, vevent, {"start_datetime": "2026-02-02"})

    assert _is_date_only(component["DTSTART"])
    assert component["DTSTART"].dt.isoformat() == "2026-02-02"
    # The untouched end keeps its DATE type too.
    assert _is_date_only(component["DTEND"])


def test_timed_event_stays_timed_when_all_day_not_passed(mocker):
    vevent = "DTSTART:20260101T090000Z\r\nDTEND:20260101T100000Z\r\n"
    component = _merge(mocker, vevent, {"start_datetime": "2026-02-02T09:00:00Z"})

    assert not _is_date_only(component["DTSTART"])


def test_stored_tzid_is_inherited_when_timezone_not_repassed(mocker):
    """Updating a TZID-bound event with a naive value used to produce floating time."""
    vtimezone = (
        "BEGIN:VTIMEZONE\r\nTZID:America/New_York\r\n"
        "BEGIN:STANDARD\r\nDTSTART:19701101T020000\r\n"
        "TZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\nTZNAME:EST\r\n"
        "END:STANDARD\r\nEND:VTIMEZONE\r\n"
    )
    vevent = (
        "DTSTART;TZID=America/New_York:20260101T090000\r\n"
        "DTEND;TZID=America/New_York:20260101T100000\r\n"
    )
    component = _merge(
        mocker, vevent, {"start_datetime": "2026-03-10T09:00:00"}, vtimezone
    )

    assert str(component["DTSTART"].dt.tzinfo) == "America/New_York"


def test_explicit_timezone_beats_inherited(mocker):
    vevent = "DTSTART;TZID=America/New_York:20260101T090000\r\n"
    component = _merge(
        mocker,
        vevent,
        {"start_datetime": "2026-03-10T09:00:00", "timezone": "Europe/Berlin"},
    )

    assert str(component["DTSTART"].dt.tzinfo) == "Europe/Berlin"


def test_explicit_offset_does_not_inherit_and_does_not_warn(mocker, caplog):
    """An offset-bearing value must not inherit the stored TZID, and must not log
    the 'ignoring timezone' warning — the caller never asked for a zone.

    The warning previously fired on *every* offset-bearing update of a TZID-bound
    event, because the stored zone was about to be passed as ``tz_name``.

    Only the wall-clock is asserted: icalendar renders a fixed-offset tzinfo as
    ``TZID="UTC-04:00"`` with no matching VTIMEZONE, so the offset is dropped on
    re-parse. That is pre-existing behaviour shared with the create path
    (``_create_ical_event``) and out of scope here — see the follow-up card.
    """
    import logging

    vevent = "DTSTART;TZID=America/New_York:20260101T090000\r\n"
    with caplog.at_level(
        logging.WARNING, logger="nextcloud_mcp_server.client.calendar"
    ):
        component = _merge(
            mocker, vevent, {"start_datetime": "2026-03-10T09:00:00-04:00"}
        )

    # Wall-clock is the caller's, and America/New_York was NOT inherited onto it.
    assert component["DTSTART"].dt.replace(tzinfo=None).isoformat() == (
        "2026-03-10T09:00:00"
    )
    assert str(component["DTSTART"].dt.tzinfo) != "America/New_York"
    assert not any("ignoring timezone" in r.message for r in caplog.records)


def test_dtstart_and_dtend_inherit_their_own_zones(mocker):
    """DTSTART and DTEND may legally carry different TZIDs; sharing DTSTART's
    would silently relocate the end of a cross-zone event."""
    vevent = (
        "DTSTART;TZID=America/New_York:20260101T090000\r\n"
        "DTEND;TZID=Europe/Berlin:20260101T180000\r\n"
    )
    component = _merge(
        mocker,
        vevent,
        {
            "start_datetime": "2026-03-10T09:00:00",
            "end_datetime": "2026-03-10T18:00:00",
        },
    )

    assert str(component["DTSTART"].dt.tzinfo) == "America/New_York"
    assert str(component["DTEND"].dt.tzinfo) == "Europe/Berlin"


def test_flip_to_all_day_without_datetimes_converts_stored_values(mocker):
    """Timed -> all-day with no new datetimes is well defined, and DTEND must be
    clamped so the DATE range isn't zero-length."""
    vevent = "DTSTART:20260101T090000Z\r\nDTEND:20260101T100000Z\r\n"
    component = _merge(mocker, vevent, {"all_day": True})

    assert _is_date_only(component["DTSTART"])
    assert _is_date_only(component["DTEND"])
    assert component["DTEND"].dt > component["DTSTART"].dt


def test_flip_with_only_one_datetime_raises(mocker):
    """A half-supplied flip would leave DTSTART/DTEND with mismatched value
    types, which is invalid iCalendar."""
    vevent = "DTSTART;VALUE=DATE:20260101\r\nDTEND;VALUE=DATE:20260102\r\n"
    client = _make_client(mocker)
    raw = _wrap_vevent(vevent)
    event_data = {"all_day": False, "start_datetime": "2026-01-01T09:00:00Z"}

    with pytest.raises(ValueError, match="requires both"):
        client._merge_ical_properties(raw, event_data, "test-event")


def test_flip_to_timed_without_datetimes_raises(mocker):
    """There is no defensible time-of-day to invent for an all-day event."""
    vevent = "DTSTART;VALUE=DATE:20260101\r\nDTEND;VALUE=DATE:20260102\r\n"
    client = _make_client(mocker)
    raw = _wrap_vevent(vevent)
    event_data = {"all_day": False}

    with pytest.raises(ValueError, match="no defensible time-of-day"):
        client._merge_ical_properties(raw, event_data, "test-event")


def test_merge_preserves_properties_absent_from_event_data(mocker):
    """The removed except-Exception fallback rebuilt the event from the partial
    update dict, destroying everything the caller didn't pass."""
    vevent = (
        "DTSTART:20260101T090000Z\r\n"
        "DTEND:20260101T100000Z\r\n"
        "LOCATION:Room 1\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=5\r\n"
        "X-CUSTOM-PROP:keep me\r\n"
    )
    component = _merge(mocker, vevent, {"title": "Renamed"})

    assert component["SUMMARY"] == "Renamed"
    assert component["LOCATION"] == "Room 1"
    assert "RRULE" in component
    assert component["X-CUSTOM-PROP"] == "keep me"
