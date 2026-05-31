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


def test_auth_username_used_for_credential_uid_for_path(mocker):
    """OIDC users: the loginName authenticates, the UID builds the DAV path.

    Nextcloud keys app-password auth on the loginName (which can differ from
    the UID), but ``/remote.php/dav/calendars/<uid>/`` must use the UID. The
    two must not be conflated.
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
    # Path identity → UID
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
