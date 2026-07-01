"""Unit tests for the CalendarClient construction path.

These pin the wiring into ``caldav.aio.AsyncDAVClient``. caldav v3.x prefers
``niquests`` over ``httpx`` and rejects ``httpx.Auth`` objects when ``niquests``
is the active backend (issue #731), so we no longer build an httpx auth object
ourselves — we pass the raw credential plus an explicit ``auth_type`` and let
caldav build whichever auth its backend needs.
"""

import pytest

pytestmark = pytest.mark.unit


def test_basic_auth_passes_password_and_auth_type_basic(mocker):
    """Password path: pass ``password=`` + ``auth_type='basic'``, no ``auth=`` arg.

    The previous wiring passed ``auth=httpx.BasicAuth(...)`` which caldav-on-niquests
    rejects with "Unexpected non-callable authentication" — the regression #731 came
    in via caldav 3.x's mandatory niquests dependency.
    """
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    CalendarClient("https://cloud.example.org", "alice", password="app-pw-1234")

    mock_dav_client.assert_called_once()
    call_kwargs = mock_dav_client.call_args.kwargs
    assert call_kwargs["url"] == "https://cloud.example.org/remote.php/dav/"
    assert call_kwargs["username"] == "alice"
    assert call_kwargs["password"] == "app-pw-1234"
    assert call_kwargs["auth_type"] == "basic"
    # Critical: no httpx.Auth object — that's what broke under niquests.
    assert "auth" not in call_kwargs


def test_token_passes_token_and_auth_type_bearer(mocker):
    """Token path: pass ``password=<token>`` + ``auth_type='bearer'``.

    caldav v3 reuses the ``password`` slot for bearer tokens — see
    ``async_davclient.build_auth_object``.
    """
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    CalendarClient("https://cloud.example.org", "alice", token="oauth-bearer-xyz")

    call_kwargs = mock_dav_client.call_args.kwargs
    assert call_kwargs["password"] == "oauth-bearer-xyz"
    assert call_kwargs["auth_type"] == "bearer"
    assert "auth" not in call_kwargs


def test_no_credentials_leaves_dav_client_unauthenticated(mocker):
    """Defensive: if neither credential is provided, don't pass any auth kwargs.

    AsyncDAVClient handles its own discovery when no auth is configured; we
    don't want to silently inject an empty password.
    """
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    CalendarClient("https://cloud.example.org", "alice")

    call_kwargs = mock_dav_client.call_args.kwargs
    assert "password" not in call_kwargs
    assert "auth_type" not in call_kwargs
    assert "auth" not in call_kwargs


def test_password_takes_precedence_over_token(mocker):
    """If a caller supplies both, password wins. Documents the precedence so a
    future caller passing both isn't surprised by which one selects auth_type.
    """
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    CalendarClient(
        "https://cloud.example.org",
        "alice",
        password="app-pw",
        token="bearer-tok",
    )

    call_kwargs = mock_dav_client.call_args.kwargs
    assert call_kwargs["password"] == "app-pw"
    assert call_kwargs["auth_type"] == "basic"


def test_auth_username_used_for_credential_uid_for_fallback_path(mocker):
    """OIDC users: the loginName authenticates, the UID seeds DAV fallback paths.

    Nextcloud keys app-password auth on the loginName (which can differ from
    the UID), but discovery starts from a UID-based calendar home fallback. The
    two identities must not be conflated.
    """
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    client = CalendarClient(
        "https://cloud.example.org",
        "Ada Lovelace",  # UID
        auth_username="ada@example.com",  # loginName
        password="app-pw-1234",
    )

    # Credential identity → loginName
    assert mock_dav_client.call_args.kwargs["username"] == "ada@example.com"
    # Fallback path identity -> UID
    assert client.username == "Ada Lovelace"
    assert (
        client._calendar_home_url
        == "https://cloud.example.org/remote.php/dav/calendars/Ada Lovelace/"
    )


def test_auth_username_defaults_to_username(mocker):
    """Backwards compat: without ``auth_username`` the UID is used for both,
    so single-user / OAuth callers (UID == loginName) are unchanged.
    """
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    CalendarClient("https://cloud.example.org", "alice", password="app-pw")

    assert mock_dav_client.call_args.kwargs["username"] == "alice"


def test_webcal_caching_header_enabled_on_client(mocker):
    """The client is constructed with the webcal-caching header turned on.

    This is what makes Nextcloud expose external subscriptions as queryable
    CachedSubscription calendars, so their events are readable through the
    normal event/search tools (issue #830).
    """
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )

    from nextcloud_mcp_server.client.calendar import CalendarClient

    CalendarClient("https://cloud.example.org", "alice", password="app-pw")

    headers = mock_dav_client.call_args.kwargs["headers"]
    assert headers["X-NC-CalDAV-Webcal-Caching"] == "On"


# --- list_calendars: regular + external subscription parsing (issue #830) ---

# A multistatus body with the calendar home, one regular calendar, and one
# external subscription (cs:subscribed) carrying a cs:source href and an
# Apple-namespace color.
_LIST_CALENDARS_MULTISTATUS = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:ical="http://apple.com/ns/ical/">
    <d:response>
        <d:href>/remote.php/dav/calendars/alice/</d:href>
        <d:propstat>
            <d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
    <d:response>
        <d:href>/remote.php/dav/calendars/alice/personal/</d:href>
        <d:propstat>
            <d:prop>
                <d:displayname>Personal</d:displayname>
                <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
                <c:calendar-description>My personal calendar</c:calendar-description>
                <cs:calendar-color>#FF0000</cs:calendar-color>
            </d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
    <d:response>
        <d:href>/remote.php/dav/calendars/alice/holidays/</d:href>
        <d:propstat>
            <d:prop>
                <d:displayname>Public Holidays</d:displayname>
                <d:resourcetype><d:collection/><cs:subscribed/></d:resourcetype>
                <ical:calendar-color>#00FF00</ical:calendar-color>
                <cs:source><d:href>https://example.com/holidays.ics</d:href></cs:source>
            </d:prop>
            <d:status>HTTP/1.1 200 OK</d:status>
        </d:propstat>
    </d:response>
</d:multistatus>"""


def _calendar_client_with_propfind(mocker, raw_xml: str):
    """Build a CalendarClient whose DAV client returns ``raw_xml`` from PROPFIND."""
    mock_dav_client = mocker.patch(
        "nextcloud_mcp_server.client.calendar.AsyncDAVClient"
    )
    instance = mock_dav_client.return_value
    instance.propfind = mocker.AsyncMock(return_value=mocker.Mock(raw=raw_xml))

    from nextcloud_mcp_server.client.calendar import CalendarClient

    client = CalendarClient("https://cloud.example.org", "alice", password="app-pw")
    return client, instance


async def test_list_calendars_includes_external_subscription(mocker):
    """External subscriptions are returned alongside regular calendars and are
    flagged read-only with their source feed URL (issue #830).
    """
    client, _ = _calendar_client_with_propfind(mocker, _LIST_CALENDARS_MULTISTATUS)

    calendars = await client.list_calendars()

    by_name = {cal["name"]: cal for cal in calendars}
    # The calendar home (plain collection) is not reported.
    assert set(by_name) == {"personal", "holidays"}

    personal = by_name["personal"]
    assert personal["display_name"] == "Personal"
    assert personal["description"] == "My personal calendar"
    assert personal["color"] == "#FF0000"
    assert personal["read_only"] is False
    assert personal["source"] is None

    holidays = by_name["holidays"]
    assert holidays["display_name"] == "Public Holidays"
    assert holidays["read_only"] is True
    assert holidays["source"] == "https://example.com/holidays.ics"
    # Subscriptions store their color under the Apple iCal namespace.
    assert holidays["color"] == "#00FF00"


async def test_list_calendars_disables_webcal_caching_for_propfind(mocker):
    """The listing PROPFIND overrides the client-wide header to "Off" so
    subscriptions surface as cs:subscribed (with a source) rather than as
    opaque regular calendars.
    """
    client, instance = _calendar_client_with_propfind(
        mocker, _LIST_CALENDARS_MULTISTATUS
    )

    await client.list_calendars()

    kwargs = instance.propfind.call_args.kwargs
    assert kwargs["headers"]["X-NC-CalDAV-Webcal-Caching"] == "Off"
    # The custom property XML must travel as ``body`` — caldav's ``props=``
    # expects a list of property names and would discard a raw XML string,
    # sending an empty <prop/> that returns neither resourcetype nor cs:source.
    assert "cs:source" in kwargs["body"]
    assert "props" not in kwargs


async def test_list_calendars_model_round_trip(mocker):
    """The dicts returned by list_calendars validate against the Calendar model,
    mirroring the server's ``Calendar(**cal_data)`` mapping.
    """
    client, _ = _calendar_client_with_propfind(mocker, _LIST_CALENDARS_MULTISTATUS)

    from nextcloud_mcp_server.models.calendar import Calendar

    calendars = [Calendar(**cal) for cal in await client.list_calendars()]
    holidays = next(c for c in calendars if c.name == "holidays")
    assert holidays.read_only is True
    assert holidays.source == "https://example.com/holidays.ics"
