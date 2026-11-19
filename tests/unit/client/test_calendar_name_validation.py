"""Calendar name validation against the user's current DAV calendar list."""

from types import SimpleNamespace

import pytest
from nextcloud_client.calendar import CalendarClient

pytestmark = pytest.mark.unit


def _client() -> CalendarClient:
    client = CalendarClient("https://cloud.example.org", "alice", password="pw")
    client._calendar_home_url = (
        "https://cloud.example.org/remote.php/dav/calendars/alice/"
    )
    return client


async def test_valid_calendar_name_passes(mocker):
    client = _client()
    mocker.patch.object(
        client,
        "list_calendars",
        return_value=[{"name": "personal"}, {"name": "studies-1"}],
    )

    await client._validate_calendar_name("studies-1")


async def test_visible_shared_calendar_name_passes(mocker):
    client = _client()
    mocker.patch.object(
        client,
        "list_calendars",
        return_value=[
            {"name": "day_shared_by_Julia Kwit", "read_only": True},
        ],
    )

    await client._validate_calendar_name("day_shared_by_Julia Kwit")


async def test_calendar_name_cache_expires_after_five_seconds(mocker):
    client = _client()
    clock = mocker.patch("nextcloud_client.calendar.time.monotonic", return_value=10.0)
    calendars = mocker.patch.object(
        client,
        "list_calendars",
        side_effect=[[{"name": "personal"}], [{"name": "personal-2"}]],
    )

    await client._validate_calendar_name("personal")
    clock.return_value = 15.001

    with pytest.raises(
        ValueError, match="Unknown calendar name 'personal'.*personal-2"
    ):
        await client._validate_calendar_name("personal")

    assert calendars.await_count == 2


async def test_bulk_search_scopes_to_validated_calendar_name(mocker):
    client = _client()
    mocker.patch.object(client, "_ensure_calendar_home", mocker.AsyncMock())
    mocker.patch.object(
        client,
        "list_calendars",
        mocker.AsyncMock(
            return_value=[{"name": "personal"}, {"name": "shared calendar"}]
        ),
    )
    events_from_calendar = mocker.patch.object(
        client, "_events_from_calendar", mocker.AsyncMock()
    )

    await client.search_events_across_calendars(filters={"calendar_name": "personal"})

    assert [call.args[0]["name"] for call in events_from_calendar.await_args_list] == [
        "personal"
    ]


async def test_bulk_search_rejects_unknown_calendar_with_valid_names(mocker):
    client = _client()
    mocker.patch.object(client, "_ensure_calendar_home", mocker.AsyncMock())
    mocker.patch.object(
        client,
        "list_calendars",
        mocker.AsyncMock(return_value=[{"name": "personal"}, {"name": "shared"}]),
    )

    with pytest.raises(
        ValueError,
        match="Unknown calendar name 'missing'.*personal, shared",
    ):
        await client.search_events_across_calendars(
            filters={"calendar_name": "missing"}
        )


async def test_unknown_calendar_name_lists_valid_names(mocker):
    client = _client()
    mocker.patch.object(
        client,
        "list_calendars",
        return_value=[{"name": "personal"}, {"name": "studies-1"}],
    )

    with pytest.raises(
        ValueError, match="Unknown calendar name 'studies'.*personal, studies-1"
    ):
        await client._validate_calendar_name("studies")


async def test_create_calendar_invalidates_name_cache(mocker):
    client = _client()
    mocker.patch.object(client, "_ensure_calendar_home", mocker.AsyncMock())
    client._calendar_names_cache = (0, frozenset({"personal"}))
    client._dav_client.mkcalendar = mocker.AsyncMock(
        return_value=SimpleNamespace(status=201)
    )
    mocker.patch.object(client, "_wait_for_calendar_propagation", mocker.AsyncMock())

    await client.create_calendar("new-calendar")

    assert client._calendar_names_cache is None


async def test_delete_calendar_invalidates_name_cache(mocker):
    client = _client()
    mocker.patch.object(client, "_ensure_calendar_home", mocker.AsyncMock())
    client._calendar_names_cache = (0, frozenset({"personal"}))
    client._dav_client.delete = mocker.AsyncMock()

    await client.delete_calendar("personal")

    assert client._calendar_names_cache is None
