"""Unit tests for the CalendarClient construction path.

These pin the wiring into ``caldav.aio.AsyncDAVClient``. caldav v3.x prefers
``niquests`` over ``httpx`` and rejects ``httpx.Auth`` objects when ``niquests``
is the active backend (issue #731), so we no longer build an httpx auth object
ourselves — we pass the raw credential plus an explicit ``auth_type`` and let
caldav build whichever auth its backend needs.
"""

import logging
from datetime import timedelta

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


# --- calendar-home-set absolute-path normalization (issue #1007) ---


def test_home_set_absolute_path_resolves_against_origin_not_subpath(mocker):
    """An absolute calendar-home-set path resolves against the origin.

    When Nextcloud is served under a subpath, calendar-home-set returns an
    absolute path that already includes that subpath (e.g.
    ``/nextcloud/remote.php/dav/calendars/David/``). Resolving it against the
    full base URL would double the subpath and yield an unroutable URL, which
    then hits Apache's default routing and 405s with an HTML body that fails
    CalDAV XML parsing (issue #1007). It must resolve against the origin only.
    """
    mocker.patch("nextcloud_mcp_server.client.calendar.AsyncDAVClient")

    from nextcloud_mcp_server.client.calendar import CalendarClient

    # No credentials needed: the method under test derives the URL purely from
    # base_url, and constructing without auth keeps this free of S2068 (hard-
    # coded credential) noise.
    client = CalendarClient("https://host/nextcloud", "David")

    home_url = client._calendar_home_url_from_home_set(
        "/nextcloud/remote.php/dav/calendars/David/"
    )

    assert home_url == "https://host/nextcloud/remote.php/dav/calendars/David/"


def test_home_set_absolute_path_resolves_against_root_origin(mocker):
    """Root-hosted deployments keep resolving absolute paths correctly."""
    mocker.patch("nextcloud_mcp_server.client.calendar.AsyncDAVClient")

    from nextcloud_mcp_server.client.calendar import CalendarClient

    client = CalendarClient("https://cloud.example.org", "alice")

    home_url = client._calendar_home_url_from_home_set(
        "/remote.php/dav/calendars/alice/"
    )

    assert home_url == "https://cloud.example.org/remote.php/dav/calendars/alice/"


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


def test_expand_without_window_returns_master_event(mocker):
    """A recurring event with no expansion window falls back to the master VEVENT.

    Guards against a revert to the `assert start_datetime is not None` that used
    to sit inside the try block, where the AssertionError was caught by
    `except Exception` and logged as a failed recurrence expansion rather than
    the missing-window caller error it is (python:S5779). The fallback value is
    identical either way — only the diagnosis differs — so the assertion here is
    that no expansion is attempted.
    """
    from nextcloud_mcp_server.client.calendar import CalendarClient

    client = CalendarClient("https://cloud.example.org", "alice", password="app-pw")
    mocker.patch.object(
        CalendarClient, "_extract_vevent_data", return_value={"uid": "master"}
    )
    expand = mocker.patch(
        "nextcloud_mcp_server.client.calendar.recurring_ical_events.of"
    )

    cal = mocker.MagicMock()
    cal.walk.return_value = [{"rrule": "FREQ=DAILY"}]

    result = client._expand_event_occurrences(
        cal, start_datetime=None, end_datetime=None, do_expand=True
    )

    assert result == [{"uid": "master"}]
    expand.assert_not_called()


# ============= Event property round-trips (GH #1251) =============
#
# These cover parameters the tools accepted but never wrote to the iCal. Every
# assertion goes through a reparse rather than a substring check, because the
# failure mode being guarded against is a property that serialises but does not
# survive being read back.


def _pure_client():
    """A CalendarClient for the pure iCal helpers.

    Built without going through ``__init__`` so no DAV client — and no
    credential — is involved; the helpers under test only touch dicts and
    icalendar objects.
    """
    from nextcloud_mcp_server.client.calendar import CalendarClient

    return CalendarClient.__new__(CalendarClient)


def _vevent(ical):
    from icalendar import Calendar as ICalendar

    return ICalendar.from_ical(ical).walk("VEVENT")[0]


def _rrule(ical):
    return _vevent(ical).get("rrule").to_ical().decode()


def _valarms(ical):
    return [sub for sub in _vevent(ical).subcomponents if sub.name == "VALARM"]


TIMED_EVENT = {
    "title": "Standup",
    "start_datetime": "2026-02-10T10:00:00Z",
    "end_datetime": "2026-02-10T11:00:00Z",
}


def test_recurrence_end_date_uses_utc_datetime_until_for_timed_events():
    """RFC 5545 §3.3.10: a DATE-TIME DTSTART requires a UTC DATE-TIME UNTIL.

    A date-only end is inclusive — bounding at midnight would drop the
    occurrence happening later that same day.
    """
    ical = _pure_client()._create_ical_event(
        {
            **TIMED_EVENT,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            "recurrence_end_date": "2026-06-30",
        },
        "uid-until-timed",
    )

    assert _rrule(ical) == "FREQ=WEEKLY;UNTIL=20260630T235959Z;BYDAY=TU"


def test_recurrence_end_date_uses_date_until_for_all_day_events():
    """A DATE-valued DTSTART requires a DATE UNTIL, not a date-time.

    Mismatched value types make clients discard the recurrence set, so the
    series silently never ends.
    """
    ical = _pure_client()._create_ical_event(
        {
            "title": "Bin day",
            "start_datetime": "2026-02-10",
            "end_datetime": "2026-02-11",
            "all_day": True,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            "recurrence_end_date": "2026-06-30",
        },
        "uid-until-allday",
    )

    assert _rrule(ical) == "FREQ=WEEKLY;UNTIL=20260630;BYDAY=TU"


@pytest.mark.parametrize("bound", ["COUNT=5", "UNTIL=20260101T000000Z"])
def test_recurrence_end_date_rejects_a_rule_that_already_bounds_itself(bound):
    """Two end conditions in one request is a contradiction, not a preference.

    RFC 5545 forbids COUNT and UNTIL together, and silently picking a winner is
    exactly the ignore-the-argument behaviour this change removes.
    """
    event_data = {
        **TIMED_EVENT,
        "recurrence_rule": f"FREQ=DAILY;{bound}",
        "recurrence_end_date": "2026-06-30",
    }

    with pytest.raises(ValueError, match="conflicts with"):
        _pure_client()._create_ical_event(event_data, "uid-conflict")


def test_recurrence_rule_applies_without_an_explicit_recurring_flag():
    """A rule is itself the intent to recur.

    Requiring ``recurring=True`` as well made the argument a no-op on create
    while update honoured it unconditionally.
    """
    ical = _pure_client()._create_ical_event(
        {**TIMED_EVENT, "recurrence_rule": "FREQ=DAILY"}, "uid-implied"
    )

    assert _rrule(ical) == "FREQ=DAILY"


def test_recurring_false_suppresses_the_rule_on_create():
    ical = _pure_client()._create_ical_event(
        {**TIMED_EVENT, "recurrence_rule": "FREQ=DAILY", "recurring": False},
        "uid-suppressed",
    )

    assert "rrule" not in _vevent(ical)


def test_update_end_date_alone_rebounds_the_stored_rule():
    """Moving the end of an already-bounded series is an edit, not a conflict."""
    client = _pure_client()
    stored = client._create_ical_event(
        {
            **TIMED_EVENT,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            "recurrence_end_date": "2026-06-30",
        },
        "uid-rebound",
    )

    moved = client._merge_ical_properties(stored, {"recurrence_end_date": "2026-07-31"})

    assert _rrule(moved) == "FREQ=WEEKLY;UNTIL=20260731T235959Z;BYDAY=TU"


def test_end_date_follows_the_dtstart_written_in_the_same_update():
    """UNTIL's value type must track the *new* DTSTART, not the replaced one.

    Flipping a timed series to all-day while setting recurrence_end_date in one
    call used to compute the value type from the stored DTSTART, because the
    recurrence block ran before ``_apply_date_updates``. The result was a DATE
    DTSTART against a date-time UNTIL — the mismatch clients silently discard.
    """
    client = _pure_client()
    stored = client._create_ical_event(
        {**TIMED_EVENT, "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU"}, "uid-flip"
    )

    flipped = client._merge_ical_properties(
        stored,
        {
            "all_day": True,
            "start_datetime": "2026-03-10",
            "end_datetime": "2026-03-11",
            "recurrence_end_date": "2026-06-30",
        },
    )

    assert _rrule(flipped) == "FREQ=WEEKLY;UNTIL=20260630;BYDAY=TU"


def test_end_date_follows_a_timed_dtstart_written_in_the_same_update():
    """The mirror case: all-day flipped to timed takes a UTC date-time UNTIL."""
    client = _pure_client()
    stored = client._create_ical_event(
        {
            "title": "Bin day",
            "start_datetime": "2026-02-10",
            "end_datetime": "2026-02-11",
            "all_day": True,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
        },
        "uid-flip-back",
    )

    flipped = client._merge_ical_properties(
        stored,
        {
            "all_day": False,
            "start_datetime": "2026-03-10T09:00:00Z",
            "end_datetime": "2026-03-10T10:00:00Z",
            "recurrence_end_date": "2026-06-30",
        },
    )

    assert _rrule(flipped) == "FREQ=WEEKLY;UNTIL=20260630T235959Z;BYDAY=TU"


def test_update_recurring_false_clears_the_series():
    """``recurring=False`` used to do nothing; only ``recurrence_rule=""`` worked."""
    client = _pure_client()
    stored = client._create_ical_event(
        {**TIMED_EVENT, "recurrence_rule": "FREQ=DAILY"}, "uid-clear"
    )

    cleared = client._merge_ical_properties(stored, {"recurring": False})

    assert "rrule" not in _vevent(cleared)


def test_color_round_trips_and_can_be_removed():
    client = _pure_client()
    stored = client._create_ical_event({**TIMED_EVENT, "color": "tomato"}, "uid-color")

    assert client._parse_ical_event(stored)["color"] == "tomato"

    recoloured = client._merge_ical_properties(stored, {"color": "slateblue"})
    assert client._parse_ical_event(recoloured)["color"] == "slateblue"

    removed = client._merge_ical_properties(stored, {"color": ""})
    assert "color" not in client._parse_ical_event(removed)


def test_hex_color_is_written_but_warns(caplog):
    """Nextcloud resolves COLOR through a CSS3 name table, so hex never renders.

    The property is still written for other CalDAV clients; the warning is what
    stops the caller believing it will show up in Nextcloud.
    """
    with caplog.at_level(logging.WARNING):
        ical = _pure_client()._create_ical_event(
            {**TIMED_EVENT, "color": "#FF0000"}, "uid-hex"
        )

    assert str(_vevent(ical).get("color")) == "#FF0000"
    assert "CSS3 colour names" in caplog.text


# ============= Ordered reminders (supersedes PR #969) =============


def test_ordered_reminders_round_trip_in_order():
    """Order is part of the contract: VALARMs come back as they were written."""
    client = _pure_client()
    ical = client._create_ical_event(
        {
            **TIMED_EVENT,
            "reminders": [
                {"action": "EMAIL", "minutes_before": 60, "description": "Prep"},
                {"action": "DISPLAY", "trigger": "-PT90S", "description": "Now"},
                {
                    "action": "DISPLAY",
                    "trigger_at": "2026-02-09T20:00:00Z",
                    "description": "Absolute",
                },
            ],
        },
        "uid-reminders",
    )

    reminders = client._parse_ical_event(ical)["reminders"]

    assert [r["description"] for r in reminders] == ["Prep", "Now", "Absolute"]
    # A whole-minute offset comes back as minutes_before; anything else keeps
    # its raw duration, so exactly one trigger field is ever present.
    assert reminders[0]["minutes_before"] == 60
    assert reminders[1]["trigger"] == "-PT1M30S"
    assert "minutes_before" not in reminders[1]
    assert reminders[2]["trigger_at"].startswith("2026-02-09T20:00:00")


def test_read_reminders_validate_as_the_reminder_model():
    """What we hand back must be accepted as input again.

    ``Reminder`` permits exactly one trigger field, so a read path emitting both
    a duration and its minute equivalent would break the round-trip.
    """
    from nextcloud_mcp_server.models.calendar import Reminder

    client = _pure_client()
    ical = client._create_ical_event(
        {
            **TIMED_EVENT,
            "reminders": [
                {"minutes_before": 30, "related": "END"},
                {"trigger": "-PT90S"},
                {"trigger_at": "2026-02-09T20:00:00Z"},
            ],
        },
        "uid-revalidate",
    )

    for reminder in client._parse_ical_event(ical)["reminders"]:
        Reminder(**reminder)


def test_email_reminder_carries_a_summary():
    """RFC 5545 §3.6.6 requires SUMMARY on an EMAIL alarm — it is the subject.

    Nextcloud addresses the message from the event's ATTENDEEs and the
    calendar's sharees, so no alarm-level ATTENDEE is needed.
    """
    ical = _pure_client()._create_ical_event(
        {**TIMED_EVENT, "reminders": [{"action": "EMAIL", "minutes_before": 15}]},
        "uid-email",
    )

    alarm = _valarms(ical)[0]
    assert str(alarm.get("action")) == "EMAIL"
    assert str(alarm.get("summary")) == "Standup"
    assert str(alarm.get("description"))


def test_related_is_omitted_from_an_absolute_trigger():
    """RELATED qualifies a duration. On a DATE-TIME trigger it is invalid iCal."""
    ical = _pure_client()._create_ical_event(
        {**TIMED_EVENT, "reminders": [{"trigger_at": "2026-02-09T20:00:00Z"}]},
        "uid-related",
    )

    trigger = _valarms(ical)[0].get("trigger")
    assert "RELATED" not in trigger.params
    assert trigger.params.get("VALUE") == "DATE-TIME"


def test_related_survives_on_a_duration_trigger():
    ical = _pure_client()._create_ical_event(
        {**TIMED_EVENT, "reminders": [{"minutes_before": 10, "related": "END"}]},
        "uid-related-duration",
    )

    assert _valarms(ical)[0].get("trigger").params.get("RELATED") == "END"


def test_reminder_email_shorthand_adds_a_second_email_alarm():
    """The legacy pair stays supported: DISPLAY, plus EMAIL at the same offset."""
    ical = _pure_client()._create_ical_event(
        {**TIMED_EVENT, "reminder_minutes": 15, "reminder_email": True},
        "uid-shorthand",
    )

    alarms = _valarms(ical)
    assert [str(a.get("action")) for a in alarms] == ["DISPLAY", "EMAIL"]
    assert {a.get("trigger").dt for a in alarms} == {timedelta(minutes=-15)}


def _shorthand_event():
    """An event carrying both shorthand alarms, as the create path builds them."""
    client = _pure_client()
    return client, client._create_ical_event(
        {**TIMED_EVENT, "reminder_minutes": 15, "reminder_email": True},
        "uid-shorthand-update",
    )


def test_reminder_email_alone_keeps_the_stored_offset():
    """Updating one shorthand field must not erase what the other one set.

    ``reminder_email`` on its own used to find no ``reminder_minutes``, clear
    every VALARM and return before adding any back — silent total loss, and
    worse than master, where the flag was merely inert.
    """
    client, stored = _shorthand_event()

    updated = client._merge_ical_properties(stored, {"reminder_email": False})

    reminders = client._parse_ical_event(updated)["reminders"]
    assert [r["action"] for r in reminders] == ["DISPLAY"]
    assert reminders[0]["minutes_before"] == 15


def test_reminder_minutes_alone_keeps_the_stored_email_alarm():
    """The mirror case: changing the offset must not drop the EMAIL alarm."""
    client, stored = _shorthand_event()

    updated = client._merge_ical_properties(stored, {"reminder_minutes": 45})

    reminders = client._parse_ical_event(updated)["reminders"]
    assert [r["action"] for r in reminders] == ["DISPLAY", "EMAIL"]
    assert {r["minutes_before"] for r in reminders} == {45}


def test_reminder_email_alone_can_add_an_email_alarm_to_a_display_only_event():
    client = _pure_client()
    stored = client._create_ical_event(
        {**TIMED_EVENT, "reminder_minutes": 20}, "uid-display-only"
    )

    updated = client._merge_ical_properties(stored, {"reminder_email": True})

    reminders = client._parse_ical_event(updated)["reminders"]
    assert [r["action"] for r in reminders] == ["DISPLAY", "EMAIL"]
    assert {r["minutes_before"] for r in reminders} == {20}


def test_reminder_minutes_zero_still_clears_everything():
    """An explicit zero is a request to remove the alarms, not a missing value."""
    client, stored = _shorthand_event()

    updated = client._merge_ical_properties(stored, {"reminder_minutes": 0})

    assert "reminders" not in client._parse_ical_event(updated)


def test_recurrence_end_date_is_utc_even_for_a_tzid_bound_dtstart():
    """RFC 5545 §3.3.10: UNTIL is UTC whenever DTSTART is a date-time.

    A TZID-bound DTSTART is the case most likely to tempt a local-time UNTIL,
    which clients would reject along with the whole recurrence set.
    """
    ical = _pure_client()._create_ical_event(
        {
            "title": "NY standup",
            "start_datetime": "2026-02-10T10:00:00",
            "end_datetime": "2026-02-10T11:00:00",
            "timezone": "America/New_York",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            "recurrence_end_date": "2026-06-30",
        },
        "uid-tzid-until",
    )

    assert str(_vevent(ical)["DTSTART"].params.get("TZID")) == "America/New_York"
    # 23:59:59 in New York on the 30th, expressed as the UTC instant RFC 5545
    # requires: 03:59:59 on July 1st.
    assert _rrule(ical) == "FREQ=WEEKLY;UNTIL=20260701T035959Z;BYDAY=TU"


def test_evening_occurrence_survives_its_own_recurrence_end_date():
    """The last occurrence must not be dropped for being late in the day.

    An evening event in a zone behind UTC has a real instant on the *following*
    UTC date. Anchoring the inclusive end-of-day to UTC midnight put the cutoff
    before that instant, silently excluding the very occurrence the caller named
    the end date to keep. 21:00 on 2026-06-30 in New York is 01:00Z on 07-01,
    which a UTC-anchored ``UNTIL=20260630T235959Z`` would have excluded.
    """
    ical = _pure_client()._create_ical_event(
        {
            "title": "Evening class",
            "start_datetime": "2026-06-02T21:00:00",
            "end_datetime": "2026-06-02T22:00:00",
            "timezone": "America/New_York",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            "recurrence_end_date": "2026-06-30",
        },
        "uid-evening",
    )

    component = _vevent(ical)
    until = component.get("rrule")["UNTIL"][0]
    last_occurrence = component["DTSTART"].dt.replace(month=6, day=30)

    assert until >= last_occurrence, (
        f"UNTIL {until} excludes the 2026-06-30 occurrence at {last_occurrence}"
    )


def test_unrelated_update_preserves_reminders_but_empty_list_clears_them():
    """Omission preserves, ``[]`` clears — the distinction the API promises."""
    client = _pure_client()
    stored = client._create_ical_event(
        {**TIMED_EVENT, "reminders": [{"minutes_before": 10}, {"minutes_before": 60}]},
        "uid-preserve",
    )

    preserved = client._merge_ical_properties(stored, {"location": "Office"})
    assert len(client._parse_ical_event(preserved)["reminders"]) == 2

    cleared = client._merge_ical_properties(stored, {"reminders": []})
    assert "reminders" not in client._parse_ical_event(cleared)


def test_todo_reminders_round_trip():
    """VTODOs had no alarm handling at all before this change."""
    client = _pure_client()
    ical = client._create_ical_todo(
        {
            "summary": "Water plants",
            "reminders": [{"trigger": "-PT6H", "description": "Soon"}],
        },
        "uid-todo-reminder",
    )

    assert client._parse_ical_todo(ical)["reminders"] == [
        {"action": "DISPLAY", "description": "Soon", "minutes_before": 360}
    ]


def test_todo_reminder_without_a_description_says_todo():
    """The per-component default has to reach the explicit reminders branch.

    ``Reminder.description`` deliberately defaults to None so the caller's
    component-specific wording wins. A model-level default would silently label
    every todo alarm "Event reminder", since the todo tools expose no
    reminder_minutes shorthand to carry the distinction.
    """
    client = _pure_client()
    ical = client._create_ical_todo(
        {"summary": "Water plants", "reminders": [{"minutes_before": 30}]},
        "uid-todo-default",
    )

    assert client._parse_ical_todo(ical)["reminders"][0]["description"] == (
        "Todo reminder"
    )


FOREIGN_ALARM_TODO = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Other Client//EN
BEGIN:VTODO
UID:uid-foreign
SUMMARY:Backup
BEGIN:VALARM
ACTION:PROCEDURE
TRIGGER;RELATED=PARENT:-PT30M
DESCRIPTION:Legacy
END:VALARM
END:VTODO
END:VCALENDAR
"""


def test_foreign_valarm_action_does_not_break_the_model(caplog):
    """Stored data comes from any CalDAV client, not just this one.

    RFC 5545 lets ACTION carry any IANA or ``X-`` token, and RELATED anything at
    all in a malformed file. Left unnormalised, either fails ``Reminder``'s
    Literal — and because ``Todo(**todo_data)`` is built in a plain list
    comprehension, that would fail the entire listing rather than the one item.
    Nextcloud discards an alarm it does not recognise; so do we.
    """
    from nextcloud_mcp_server.models.calendar import Todo

    with caplog.at_level(logging.WARNING):
        todo_data = _pure_client()._parse_ical_todo(FOREIGN_ALARM_TODO)

    assert todo_data["reminders"] == [
        {"action": "DISPLAY", "description": "Legacy", "minutes_before": 30}
    ]
    assert "PROCEDURE" in caplog.text

    # The model is what actually crashed the listing, so build it.
    todo = Todo(**todo_data)
    assert todo.reminders[0].action == "DISPLAY"
    assert todo.reminders[0].related is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"minutes_before": 5, "trigger": "-PT5M"},
        {"trigger_at": "2026-01-01T00:00:00Z", "related": "END"},
    ],
    ids=["no-trigger", "two-triggers", "related-with-absolute"],
)
def test_reminder_model_rejects_incoherent_triggers(payload):
    from pydantic import ValidationError

    from nextcloud_mcp_server.models.calendar import Reminder

    with pytest.raises(ValidationError):
        Reminder(**payload)
