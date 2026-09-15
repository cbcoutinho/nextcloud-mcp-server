"""Free/busy transparency must survive the trip from CalDAV to the model.

Two independent switches decide whether something consumes time:

* the calendar's ``schedule-calendar-transp`` (RFC 4791 5.2.9) -- Nextcloud
  calls it "never show me as busy";
* the event's ``TRANSP`` (RFC 5545 3.8.2.7) -- shown as Busy/Free per event.

Neither was surfaced, so any consumer computing availability had to treat
birthdays, subscribed holiday feeds and explicitly free events as busy.
"""

import pytest

from nextcloud_mcp_server.client.calendar import _calendar_is_transparent
from nextcloud_mcp_server.models.calendar import Calendar, CalendarEventSummary

pytestmark = pytest.mark.unit


class TestCalendarModel:
    def test_defaults_to_opaque(self):
        cal = Calendar(name="work", display_name="Work")
        assert cal.transparent is False

    def test_transparent_round_trips(self):
        cal = Calendar(name="kids", display_name="Kids", transparent=True)
        assert cal.transparent is True


class TestEventModel:
    def test_transp_is_optional(self):
        event = CalendarEventSummary(uid="1", summary="s", start="2026-09-07T09:00:00")
        assert event.transp is None

    @pytest.mark.parametrize("value", ["OPAQUE", "TRANSPARENT"])
    def test_transp_round_trips(self, value):
        event = CalendarEventSummary(
            uid="1", summary="s", start="2026-09-07T09:00:00", transp=value
        )
        assert event.transp == value


class TestPropfindParsing:
    """The calendar property is an element, not text."""

    NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}

    @staticmethod
    def _parse(fragment: str):
        from lxml import etree

        return etree.fromstring(fragment.encode("utf-8"))

    def test_transparent_element_detected(self):
        elem = self._parse(
            '<d:prop xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<c:schedule-calendar-transp><c:transparent/>"
            "</c:schedule-calendar-transp></d:prop>"
        )
        assert _calendar_is_transparent(elem, self.NS) is True

    def test_opaque_element_is_not_transparent(self):
        elem = self._parse(
            '<d:prop xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<c:schedule-calendar-transp><c:opaque/>"
            "</c:schedule-calendar-transp></d:prop>"
        )
        assert _calendar_is_transparent(elem, self.NS) is False

    def test_missing_property_is_not_transparent(self):
        elem = self._parse(
            '<d:prop xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"/>'
        )
        assert _calendar_is_transparent(elem, self.NS) is False


class TestEventTransp:
    """TRANSP is read off the VEVENT, defaulting to OPAQUE when absent."""

    @staticmethod
    def _extract(ics: str) -> dict:
        from icalendar import Calendar as ICalendar

        from nextcloud_mcp_server.client.calendar import CalendarClient

        cal = ICalendar.from_ical(ics)
        vevent = next(c for c in cal.walk() if c.name == "VEVENT")
        # The extractor only reads from the component; skip __init__ so the
        # test does not need a live DAV client.
        client = CalendarClient.__new__(CalendarClient)
        return client._extract_vevent_data(vevent)

    ICS = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
        "BEGIN:VEVENT\r\nUID:1\r\nSUMMARY:s\r\n"
        "DTSTART:20260907T090000Z\r\nDTEND:20260907T100000Z\r\n"
        "{extra}END:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    def test_absent_transp_defaults_to_opaque(self):
        assert self._extract(self.ICS.format(extra=""))["transp"] == "OPAQUE"

    def test_transparent_is_read(self):
        data = self._extract(self.ICS.format(extra="TRANSP:TRANSPARENT\r\n"))
        assert data["transp"] == "TRANSPARENT"

    def test_opaque_is_read(self):
        data = self._extract(self.ICS.format(extra="TRANSP:OPAQUE\r\n"))
        assert data["transp"] == "OPAQUE"
