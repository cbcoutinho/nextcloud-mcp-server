"""Unit coverage for DAV current-user-principal path discovery."""

from types import SimpleNamespace

import httpx
import pytest
from caldav.lib import error as caldav_error

from nextcloud_mcp_server.client.contacts import ContactsClient
from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit


def _http_response(content: bytes = b"", status_code: int = 207) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=content,
        request=httpx.Request("PROPFIND", "https://cloud.example.org/remote.php/dav/"),
    )


def _principal_body(principal_id: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
    <d:response>
        <d:propstat>
            <d:prop>
                <d:current-user-principal>
                    <d:href>/remote.php/dav/principals/users/{principal_id}/</d:href>
                </d:current-user-principal>
            </d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
</d:multistatus>""".encode()


def _principal_body_without_href() -> bytes:
    return b"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
    <d:response>
        <d:propstat>
            <d:prop>
                <d:current-user-principal/>
            </d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
</d:multistatus>"""


def _empty_webdav_dir(principal_id: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
    <d:response>
        <d:href>/remote.php/dav/files/{principal_id}/</d:href>
        <d:propstat>
            <d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
</d:multistatus>""".encode()


def _addressbooks_body(principal_id: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">
    <d:response>
        <d:href>/remote.php/dav/addressbooks/users/{principal_id}/contacts/</d:href>
        <d:propstat>
            <d:prop>
                <d:displayname>Contacts</d:displayname>
                <d:getctag>ctag-1</d:getctag>
            </d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
</d:multistatus>""".encode()


def _calendar_multistatus(principal_id: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/" xmlns:c="urn:ietf:params:xml:ns:caldav">
    <d:response>
        <d:href>/remote.php/dav/calendars/{principal_id}/</d:href>
        <d:propstat>
            <d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
    <d:response>
        <d:href>/remote.php/dav/calendars/{principal_id}/personal/</d:href>
        <d:propstat>
            <d:prop>
                <d:displayname>Personal</d:displayname>
                <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
                <cs:calendar-color>#1976D2</cs:calendar-color>
            </d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
</d:multistatus>"""


def _request_error() -> httpx.RequestError:
    request = httpx.Request("PROPFIND", "https://cloud.example.org/remote.php/dav/")
    return httpx.RequestError("temporary DAV discovery failure", request=request)


async def test_webdav_public_method_discovers_divergent_principal(mocker):
    client = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    client._make_request = mocker.AsyncMock(
        side_effect=[
            _http_response(_principal_body("alice_1234")),
            _http_response(_empty_webdav_dir("alice_1234")),
        ]
    )

    await client.list_directory("Documents")

    calls = client._make_request.await_args_list
    assert calls[0].args[:2] == ("PROPFIND", "/remote.php/dav/")
    assert calls[1].args[:2] == (
        "PROPFIND",
        "/remote.php/dav/files/alice_1234/Documents/",
    )


def test_webdav_search_scope_uses_discovered_principal(mocker):
    client = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    client._principal_id = "alice_1234"

    body = client._build_search_xml(
        scope="Reports",
        where_conditions=None,
        properties=["displayname"],
        order_by=None,
        limit=None,
    )

    assert "<d:href>/files/alice_1234/Reports</d:href>" in body


async def test_webdav_equal_principal_keeps_username_path(mocker):
    client = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    file_response = _http_response(b"hello", status_code=200)
    file_response.headers["content-type"] = "text/plain"
    client._make_request = mocker.AsyncMock(
        side_effect=[_http_response(_principal_body("alice")), file_response]
    )

    content, _ = await client.read_file("notes.txt")

    assert content == b"hello"
    calls = client._make_request.await_args_list
    assert calls[0].args[:2] == ("PROPFIND", "/remote.php/dav/")
    assert calls[1].args[:2] == ("GET", "/remote.php/dav/files/alice/notes.txt")


async def test_webdav_discovery_failure_falls_back_to_username(mocker):
    client = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    file_response = _http_response(b"fallback", status_code=200)
    file_response.headers["content-type"] = "text/plain"
    client._make_request = mocker.AsyncMock(
        side_effect=[_request_error(), file_response]
    )

    content, _ = await client.read_file("notes.txt")

    assert content == b"fallback"
    calls = client._make_request.await_args_list
    assert calls[0].args[:2] == ("PROPFIND", "/remote.php/dav/")
    assert calls[1].args[:2] == ("GET", "/remote.php/dav/files/alice/notes.txt")


async def test_webdav_successful_discovery_is_cached_per_instance(mocker):
    client = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    client._make_request = mocker.AsyncMock(
        side_effect=[
            _http_response(_principal_body("alice_1234")),
            _http_response(_empty_webdav_dir("alice_1234")),
            _http_response(_empty_webdav_dir("alice_1234")),
        ]
    )

    await client.list_directory("one")
    await client.list_directory("two")

    calls = client._make_request.await_args_list
    assert [call.args[:2] for call in calls] == [
        ("PROPFIND", "/remote.php/dav/"),
        ("PROPFIND", "/remote.php/dav/files/alice_1234/one/"),
        ("PROPFIND", "/remote.php/dav/files/alice_1234/two/"),
    ]


async def test_webdav_principal_href_is_unquoted(mocker):
    client = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    file_response = _http_response(b"mailbox", status_code=200)
    file_response.headers["content-type"] = "text/plain"
    client._make_request = mocker.AsyncMock(
        side_effect=[
            _http_response(_principal_body("alice%40example.com")),
            file_response,
        ]
    )

    content, _ = await client.read_file("notes.txt")

    assert content == b"mailbox"
    assert client._principal_id == "alice@example.com"
    assert client._make_request.await_args_list[1].args[:2] == (
        "GET",
        "/remote.php/dav/files/alice@example.com/notes.txt",
    )


async def test_webdav_missing_principal_href_falls_back_without_caching(mocker):
    client = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    first_file = _http_response(b"fallback", status_code=200)
    first_file.headers["content-type"] = "text/plain"
    second_file = _http_response(b"discovered", status_code=200)
    second_file.headers["content-type"] = "text/plain"
    client._make_request = mocker.AsyncMock(
        side_effect=[
            _http_response(_principal_body_without_href()),
            first_file,
            _http_response(_principal_body("alice_1234")),
            second_file,
        ]
    )

    first_content, _ = await client.read_file("one.txt")
    second_content, _ = await client.read_file("two.txt")

    assert first_content == b"fallback"
    assert second_content == b"discovered"
    calls = client._make_request.await_args_list
    assert [call.args[:2] for call in calls] == [
        ("PROPFIND", "/remote.php/dav/"),
        ("GET", "/remote.php/dav/files/alice/one.txt"),
        ("PROPFIND", "/remote.php/dav/"),
        ("GET", "/remote.php/dav/files/alice_1234/two.txt"),
    ]


async def test_carddav_public_method_discovers_divergent_principal(mocker):
    client = ContactsClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")
    client._make_request = mocker.AsyncMock(
        side_effect=[
            _http_response(_principal_body("alice_1234")),
            _http_response(_addressbooks_body("alice_1234")),
        ]
    )

    addressbooks = await client.list_addressbooks()

    assert addressbooks[0]["name"] == "contacts"
    calls = client._make_request.await_args_list
    assert calls[0].args[:2] == ("PROPFIND", "/remote.php/dav/")
    assert calls[1].args[:2] == (
        "PROPFIND",
        "/remote.php/dav/addressbooks/users/alice_1234",
    )


def _calendar_client_with_principal(
    mocker, principal_id: str, *, calendar_home_id: str | None = None
):
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )
    dav_client = mock_dav_client.return_value
    principal = SimpleNamespace(
        url=f"https://cloud.example.org/remote.php/dav/principals/users/{principal_id}/"
    )
    if calendar_home_id is not None:
        principal.calendar_home_set = SimpleNamespace(
            url=f"https://cloud.example.org/remote.php/dav/calendars/{calendar_home_id}/"
        )
    dav_client.get_principal = mocker.AsyncMock(return_value=principal)
    response_id = calendar_home_id or principal_id
    dav_client.propfind = mocker.AsyncMock(
        return_value=mocker.Mock(raw=_calendar_multistatus(response_id))
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    client = CalendarClient("https://cloud.example.org", "alice", password="app-pw")
    return client, dav_client


async def test_caldav_list_calendars_discovers_divergent_principal(mocker):
    client, dav_client = _calendar_client_with_principal(mocker, "alice_1234")

    calendars = await client.list_calendars()

    assert [calendar["name"] for calendar in calendars] == ["personal"]
    dav_client.get_principal.assert_awaited_once()
    dav_client.propfind.assert_awaited_once()
    assert (
        dav_client.propfind.await_args.args[0]
        == "https://cloud.example.org/remote.php/dav/calendars/alice_1234/"
    )


async def test_caldav_prefers_discovered_calendar_home_set(mocker):
    client, dav_client = _calendar_client_with_principal(
        mocker, "alice_1234", calendar_home_id="calendar_home_5678"
    )

    calendars = await client.list_calendars()

    assert [calendar["name"] for calendar in calendars] == ["personal"]
    dav_client.get_principal.assert_awaited_once()
    assert (
        dav_client.propfind.await_args.args[0]
        == "https://cloud.example.org/remote.php/dav/calendars/calendar_home_5678/"
    )


async def test_caldav_uses_async_safe_calendar_home_property_lookup(mocker):
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )
    dav_client = mock_dav_client.return_value

    class AsyncPrincipal:
        url = "https://cloud.example.org/remote.php/dav/principals/users/alice_1234/"

        async def get_property(self, prop):
            return "/remote.php/dav/calendars/calendar_home_5678/"

        @property
        def calendar_home_set(self):
            raise TypeError("argument of type 'coroutine' is not iterable")

    dav_client.get_principal = mocker.AsyncMock(return_value=AsyncPrincipal())
    dav_client.propfind = mocker.AsyncMock(
        return_value=mocker.Mock(raw=_calendar_multistatus("calendar_home_5678"))
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    client = CalendarClient("https://cloud.example.org", "alice", password="app-pw")

    await client.list_calendars()

    assert (
        dav_client.propfind.await_args.args[0]
        == "https://cloud.example.org/remote.php/dav/calendars/calendar_home_5678/"
    )


async def test_caldav_falls_back_to_calendar_home_property_when_lookup_empty(mocker):
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )
    dav_client = mock_dav_client.return_value

    class Principal:
        url = "https://cloud.example.org/remote.php/dav/principals/users/alice_1234/"

        async def get_property(self, prop):
            return None

        @property
        def calendar_home_set(self):
            return SimpleNamespace(
                url="https://cloud.example.org/remote.php/dav/calendars/calendar_home_5678/"
            )

    dav_client.get_principal = mocker.AsyncMock(return_value=Principal())
    dav_client.propfind = mocker.AsyncMock(
        return_value=mocker.Mock(raw=_calendar_multistatus("calendar_home_5678"))
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    client = CalendarClient("https://cloud.example.org", "alice", password="app-pw")

    await client.list_calendars()

    assert (
        dav_client.propfind.await_args.args[0]
        == "https://cloud.example.org/remote.php/dav/calendars/calendar_home_5678/"
    )


async def test_caldav_discovery_failure_falls_back_to_username(mocker):
    client, dav_client = _calendar_client_with_principal(mocker, "alice")
    dav_client.get_principal.side_effect = caldav_error.DAVError("temporary failure")

    calendars = await client.list_calendars()

    assert [calendar["name"] for calendar in calendars] == ["personal"]
    dav_client.get_principal.assert_awaited_once()
    assert (
        dav_client.propfind.await_args.args[0]
        == "https://cloud.example.org/remote.php/dav/calendars/alice/"
    )


async def test_caldav_discovery_failure_retries_on_next_call(mocker):
    client, dav_client = _calendar_client_with_principal(mocker, "alice_1234")
    principal = dav_client.get_principal.return_value
    dav_client.get_principal.side_effect = [
        caldav_error.DAVError("temporary failure"),
        principal,
    ]
    dav_client.propfind.side_effect = [
        mocker.Mock(raw=_calendar_multistatus("alice")),
        mocker.Mock(raw=_calendar_multistatus("alice_1234")),
    ]

    first = await client.list_calendars()
    second = await client.list_calendars()

    assert [calendar["name"] for calendar in first] == ["personal"]
    assert [calendar["name"] for calendar in second] == ["personal"]
    assert dav_client.get_principal.await_count == 2
    assert [call.args[0] for call in dav_client.propfind.await_args_list] == [
        "https://cloud.example.org/remote.php/dav/calendars/alice/",
        "https://cloud.example.org/remote.php/dav/calendars/alice_1234/",
    ]


async def test_caldav_create_calendar_uses_discovered_home_url(mocker):
    client, dav_client = _calendar_client_with_principal(mocker, "alice_1234")
    dav_client.mkcalendar = mocker.AsyncMock(return_value=SimpleNamespace(status=201))
    client._wait_for_calendar_propagation = mocker.AsyncMock()

    await client.create_calendar("team")

    dav_client.get_principal.assert_awaited_once()
    assert (
        dav_client.mkcalendar.await_args.args[0]
        == "https://cloud.example.org/remote.php/dav/calendars/alice_1234/team/"
    )


async def test_caldav_delete_calendar_uses_discovered_home_url(mocker):
    client, dav_client = _calendar_client_with_principal(mocker, "alice_1234")
    dav_client.delete = mocker.AsyncMock()

    await client.delete_calendar("team")

    dav_client.get_principal.assert_awaited_once()
    dav_client.delete.assert_awaited_once_with(
        "https://cloud.example.org/remote.php/dav/calendars/alice_1234/team/"
    )


async def test_caldav_event_operations_use_discovered_home_url(mocker):
    client, dav_client = _calendar_client_with_principal(mocker, "alice_1234")
    fake_calendar = mocker.Mock()
    fake_calendar.save_event = mocker.AsyncMock(
        return_value=SimpleNamespace(url="https://cloud.example.org/event.ics")
    )
    mock_calendar = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncCalendar",
        return_value=fake_calendar,
    )

    await client.create_event("team", {"title": "Planning"})

    dav_client.get_principal.assert_awaited_once()
    assert (
        mock_calendar.call_args.kwargs["url"]
        == "https://cloud.example.org/remote.php/dav/calendars/alice_1234/team/"
    )
