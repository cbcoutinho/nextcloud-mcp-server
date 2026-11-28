"""Free/busy transparency must survive the trip from CalDAV to the model.

Two independent switches decide whether something consumes time:

* the calendar's ``schedule-calendar-transp`` (RFC 4791 5.2.9) -- Nextcloud
  calls it "never show me as busy";
* the event's ``TRANSP`` (RFC 5545 3.8.2.7) -- shown as Busy/Free per event.

Neither was surfaced, so any consumer computing availability had to treat
birthdays, subscribed holiday feeds and explicitly free events as busy.
"""

import pytest
from nextcloud_client.calendar import _calendar_is_transparent

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
        from nextcloud_client.calendar import CalendarClient

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


# ============= Writing transparency (GH #1604) =============


def _bare_client():
    """A CalendarClient without __init__, so no live DAV client is needed."""
    from nextcloud_client.calendar import CalendarClient

    return CalendarClient.__new__(CalendarClient)


def _vevent(ics: str):
    from icalendar import Calendar as ICalendar

    return next(c for c in ICalendar.from_ical(ics).walk() if c.name == "VEVENT")


class TestNormalizeTransparency:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("OPAQUE", "OPAQUE"),
            ("TRANSPARENT", "TRANSPARENT"),
            ("transparent", "TRANSPARENT"),
            (" Opaque ", "OPAQUE"),
        ],
    )
    def test_accepts_both_values_in_any_case(self, value, expected):
        from nextcloud_client.calendar import normalize_transparency

        assert normalize_transparency(value) == expected

    @pytest.mark.parametrize("value", ["", "FREE", "BUSY", "TRANSP"])
    def test_rejects_anything_else(self, value):
        from nextcloud_client.calendar import normalize_transparency

        with pytest.raises(ValueError, match="OPAQUE or TRANSPARENT"):
            normalize_transparency(value)


class TestEventTranspWrite:
    """create and update write TRANSP; the read path then reports it."""

    EVENT = {
        "title": "Due date",
        "start_datetime": "2026-09-07T09:00:00Z",
        "end_datetime": "2026-09-07T10:00:00Z",
    }

    @pytest.mark.parametrize("value", ["OPAQUE", "TRANSPARENT"])
    def test_create_writes_transp(self, value):
        client = _bare_client()
        ics = client._create_ical_event({**self.EVENT, "transparency": value}, "u1")

        assert client._extract_vevent_data(_vevent(ics))["transp"] == value

    def test_create_without_transparency_writes_no_transp(self):
        client = _bare_client()
        ics = client._create_ical_event(self.EVENT, "u1")

        assert "TRANSP" not in _vevent(ics)

    def test_update_sets_transp(self):
        client = _bare_client()
        stored = client._create_ical_event(self.EVENT, "u1")

        merged = client._merge_ical_properties(stored, {"transparency": "TRANSPARENT"})

        assert str(_vevent(merged)["TRANSP"]) == "TRANSPARENT"

    def test_update_replaces_stored_transp(self):
        client = _bare_client()
        stored = client._create_ical_event(
            {**self.EVENT, "transparency": "TRANSPARENT"}, "u1"
        )

        merged = client._merge_ical_properties(stored, {"transparency": "opaque"})

        assert str(_vevent(merged)["TRANSP"]) == "OPAQUE"

    def test_update_without_transparency_keeps_stored_transp(self):
        client = _bare_client()
        stored = client._create_ical_event(
            {**self.EVENT, "transparency": "TRANSPARENT"}, "u1"
        )

        merged = client._merge_ical_properties(stored, {"title": "Renamed"})

        assert str(_vevent(merged)["TRANSP"]) == "TRANSPARENT"

    def test_invalid_value_is_refused(self):
        client = _bare_client()
        stored = client._create_ical_event(self.EVENT, "u1")
        event_data = {"transparency": "FREE"}

        with pytest.raises(ValueError, match="OPAQUE or TRANSPARENT"):
            client._merge_ical_properties(stored, event_data)


class TestCalendarRequestBodies:
    """MKCALENDAR and PROPPATCH write schedule-calendar-transp as an element."""

    NS = {
        "d": "DAV:",
        "c": "urn:ietf:params:xml:ns:caldav",
        "ical": "http://apple.com/ns/ical/",  # NOSONAR(S5332) XML namespace URI
    }

    @staticmethod
    def _prop(body: str):
        from lxml import etree

        return etree.fromstring(body.encode("utf-8")).find(
            "./d:set/d:prop", TestCalendarRequestBodies.NS
        )

    @pytest.mark.parametrize(
        ("value", "transparent"), [("TRANSPARENT", True), ("OPAQUE", False)]
    )
    def test_mkcalendar_writes_transparency(self, value, transparent):
        from nextcloud_client.calendar import mkcalendar_body

        prop = self._prop(mkcalendar_body(display_name="Info", transparency=value))

        assert prop.find("c:schedule-calendar-transp", self.NS) is not None
        assert _calendar_is_transparent(prop, self.NS) is transparent

    def test_mkcalendar_without_transparency_leaves_the_default(self):
        from nextcloud_client.calendar import mkcalendar_body

        prop = self._prop(mkcalendar_body(display_name="Info"))

        assert prop.find("c:schedule-calendar-transp", self.NS) is None
        comps = prop.findall("c:supported-calendar-component-set/c:comp", self.NS)
        assert [c.get("name") for c in comps] == ["VEVENT", "VTODO"]

    def test_mkcalendar_escapes_text(self):
        """A name with & or < used to produce an invalid MKCALENDAR body."""
        from nextcloud_client.calendar import mkcalendar_body

        prop = self._prop(
            mkcalendar_body(display_name="R&D <team>", description="a & b")
        )

        assert prop.findtext("d:displayname", namespaces=self.NS) == "R&D <team>"
        assert prop.findtext("c:calendar-description", namespaces=self.NS) == "a & b"

    def test_proppatch_sets_only_given_properties(self):
        from nextcloud_client.calendar import calendar_proppatch_body

        prop = self._prop(calendar_proppatch_body(transparency="TRANSPARENT"))

        assert [child.tag for child in prop] == [
            "{urn:ietf:params:xml:ns:caldav}schedule-calendar-transp"
        ]
        assert _calendar_is_transparent(prop, self.NS) is True

    def test_proppatch_sets_all_four(self):
        from nextcloud_client.calendar import calendar_proppatch_body

        prop = self._prop(
            calendar_proppatch_body(
                display_name="Info",
                description="",
                color="#FF0000",
                transparency="OPAQUE",
            )
        )

        assert prop.findtext("d:displayname", namespaces=self.NS) == "Info"
        assert prop.find("c:calendar-description", self.NS) is not None
        assert prop.findtext("ical:calendar-color", namespaces=self.NS) == "#FF0000"
        assert _calendar_is_transparent(prop, self.NS) is False


MULTISTATUS = (
    '<?xml version="1.0"?>'
    '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:response><d:href>/remote.php/dav/calendars/alice/info/</d:href>{propstats}"
    "</d:response></d:multistatus>"
)


def _propstat(prop: str, status: str) -> str:
    return (
        f"<d:propstat><d:prop>{prop}</d:prop>"
        f"<d:status>HTTP/1.1 {status}</d:status></d:propstat>"
    )


class TestProppatchFailures:
    def test_all_ok_is_no_failure(self):
        from nextcloud_client.calendar import proppatch_failures

        raw = MULTISTATUS.format(
            propstats=_propstat(
                "<d:displayname/><c:schedule-calendar-transp/>", "200 OK"
            )
        )

        assert proppatch_failures(raw) == []

    def test_refused_properties_are_listed(self):
        from nextcloud_client.calendar import proppatch_failures

        raw = MULTISTATUS.format(
            propstats=_propstat("<c:schedule-calendar-transp/>", "403 Forbidden")
            + _propstat("<d:displayname/>", "424 Failed Dependency")
        )

        assert proppatch_failures(raw) == [
            "schedule-calendar-transp: HTTP/1.1 403 Forbidden",
            "displayname: HTTP/1.1 424 Failed Dependency",
        ]


class TestUpdateCalendar:
    HOME = "https://nc.example.com/remote.php/dav/calendars/alice/"

    @pytest.fixture
    def client(self, mocker):
        client = _bare_client()
        client._calendar_home_url = self.HOME
        client._ensure_calendar_home = mocker.AsyncMock()
        client._dav_client = mocker.MagicMock()
        client._dav_client.proppatch = mocker.AsyncMock(
            return_value=mocker.MagicMock(
                status=207,
                raw=MULTISTATUS.format(
                    propstats=_propstat("<c:schedule-calendar-transp/>", "200 OK")
                ),
            )
        )
        client.list_calendars = mocker.AsyncMock(
            return_value=[
                {"name": "work", "display_name": "Work", "transparent": False},
                {"name": "info", "display_name": "Info", "transparent": True},
            ]
        )
        return client

    async def test_sends_one_proppatch_and_returns_the_reread_calendar(self, client):
        result = await client.update_calendar("info", transparency="TRANSPARENT")

        assert result == {"name": "info", "display_name": "Info", "transparent": True}
        url, body = client._dav_client.proppatch.call_args.args
        assert url == f"{self.HOME}info/"
        assert "<c:transparent/>" in body
        assert "displayname" not in body

    async def test_refused_property_raises(self, client, mocker):
        client._dav_client.proppatch.return_value = mocker.MagicMock(
            status=207,
            raw=MULTISTATUS.format(
                propstats=_propstat("<c:schedule-calendar-transp/>", "403 Forbidden")
            ),
        )

        with pytest.raises(RuntimeError, match="403 Forbidden"):
            await client.update_calendar("info", transparency="TRANSPARENT")

    async def test_http_error_raises(self, client, mocker):
        client._dav_client.proppatch.return_value = mocker.MagicMock(status=404, raw="")

        with pytest.raises(RuntimeError, match="HTTP 404"):
            await client.update_calendar("missing", color="#000000")

    async def test_nothing_to_update_sends_nothing(self, client):
        with pytest.raises(ValueError, match="No calendar property"):
            await client.update_calendar("info")

        client._dav_client.proppatch.assert_not_called()
