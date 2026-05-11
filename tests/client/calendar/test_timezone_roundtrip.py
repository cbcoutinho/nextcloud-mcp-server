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
