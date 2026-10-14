"""Calendar query boundaries and authoritative, constant-cost display names."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from caldav.response import DAVResponse
from lxml import etree

from nextcloud_mcp_server.client.calendar import CalendarClient

pytestmark = pytest.mark.unit


@pytest.fixture
def client(mocker):
    client = CalendarClient("https://cloud.example.org", "alice")
    mocker.patch.object(client, "_ensure_calendar_home", return_value=None)
    return client


async def test_report_serializes_local_bounds_as_utc(client, mocker) -> None:
    calendar = client._get_calendar("1")
    report = mocker.patch.object(
        client._dav_client,
        "report",
        return_value=DAVResponse.from_bytes(b'<d:multistatus xmlns:d="DAV:"/>'),
    )
    tz = ZoneInfo("Asia/Seoul")
    await client._search_events_by_date(
        calendar,
        dt.datetime(2026, 9, 15, tzinfo=tz),
        dt.datetime(2026, 9, 16, tzinfo=tz),
    )
    query = etree.fromstring(report.call_args.args[1])
    time_range = query.find(".//{urn:ietf:params:xml:ns:caldav}time-range")
    assert time_range.attrib == {"start": "20260914T150000Z", "end": "20260915T150000Z"}


@pytest.mark.parametrize(
    "method", ["get_calendar_events", "search_events_across_calendars"]
)
async def test_invalid_client_bounds_fail_before_discovery(client, method: str) -> None:
    query = getattr(client, method)
    kwargs = {"calendar_name": "1"} if method == "get_calendar_events" else {}
    start = dt.datetime(2026, 9, 16)
    end = dt.datetime(2026, 9, 15, tzinfo=dt.UTC)
    with pytest.raises(ValueError, match="start_datetime"):
        await query(start_datetime=start, end_datetime=end, **kwargs)
    client._ensure_calendar_home.assert_not_awaited()


async def test_report_and_expansion_share_normalized_bounds(client, mocker) -> None:
    event = mocker.MagicMock(data="BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")
    search = mocker.patch.object(client, "_search_events_by_date", return_value=[event])
    expand = mocker.patch.object(client, "_expand_event_occurrences", return_value=[])
    start = dt.datetime(2026, 9, 15)
    end = dt.datetime(2026, 9, 16, tzinfo=ZoneInfo("Asia/Seoul"))
    await client.get_calendar_events("1", start, end)
    assert search.call_args.args[1:] == (
        start.replace(tzinfo=dt.UTC),
        dt.datetime(2026, 9, 15, 15, tzinfo=dt.UTC),
    )
    assert expand.call_args.args[1:] == (*search.call_args.args[1:], True)


@pytest.mark.parametrize(
    "display_name,expected", [("My Calendar", "My Calendar"), ("", "1")]
)
async def test_display_name_reads_dav_instead_of_seeded_slug(
    client, mocker, display_name: str, expected: str
) -> None:
    response = f"""<d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/remote.php/dav/calendars/alice/1/</d:href>
        <d:propstat><d:prop><d:displayname>{display_name}</d:displayname></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
      </d:response></d:multistatus>""".encode()
    propfind = mocker.patch.object(
        client._dav_client, "propfind", return_value=DAVResponse.from_bytes(response)
    )
    assert await client.get_calendar_display_name("1") == expected
    propfind.assert_awaited_once()
    assert (
        str(propfind.call_args.args[0])
        == "https://cloud.example.org/remote.php/dav/calendars/alice/1/"
    )
    assert propfind.call_args.args[2] == 0
