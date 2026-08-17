"""Integration tests for CalDAV ETag concurrency against a live Nextcloud.

The unit tests pin the wiring against mocks. These prove the contract holds
against the real server: that Nextcloud hands back the ETags we now surface,
that a conditional write advances them, and -- the point of the whole exercise
-- that a write carrying a stale ETag is actually *prevented* rather than
silently clobbering the newer version.
"""

import logging
import uuid
from datetime import datetime, timedelta

import pytest

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.client.dav_errors import DavPreconditionFailed

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration


@pytest.fixture
async def etag_test_event(nc_client: NextcloudClient, temporary_calendar: str):
    """Create an event and clean it up afterwards."""
    tomorrow = datetime.now() + timedelta(days=1)
    result = await nc_client.calendar.create_event(
        temporary_calendar,
        {
            "title": f"etag concurrency {uuid.uuid4().hex[:8]}",
            "start_datetime": tomorrow.strftime("%Y-%m-%dT10:00:00"),
            "end_datetime": tomorrow.strftime("%Y-%m-%dT11:00:00"),
        },
    )

    yield temporary_calendar, result

    try:
        await nc_client.calendar.delete_event(temporary_calendar, result["uid"])
    except Exception as e:
        logger.warning("Cleanup failed for event %s: %s", result["uid"], e)


async def test_reads_surface_a_real_etag(nc_client: NextcloudClient, etag_test_event):
    """create/get return the server's ETag, not the empty string they used to."""
    calendar_name, created = etag_test_event

    assert created["etag"], "create_event returned no etag"

    _, read_etag = await nc_client.calendar.get_event(calendar_name, created["uid"])

    assert read_etag == created["etag"]


async def test_stale_etag_write_is_refused_and_changes_nothing(
    nc_client: NextcloudClient, etag_test_event
):
    """The lost-update scenario the fix exists to prevent.

    Two writers read the same ETag. The first write wins and advances it. The
    second, still holding the original, must be refused -- and crucially the
    first writer's content must survive, which is exactly what last-write-wins
    used to destroy.
    """
    calendar_name, created = etag_test_event
    uid = created["uid"]
    _, shared_etag = await nc_client.calendar.get_event(calendar_name, uid)

    # Writer A commits against the shared etag.
    first = await nc_client.calendar.update_event(
        calendar_name, uid, {"title": "writer A"}, shared_etag
    )
    assert first["etag"] and first["etag"] != shared_etag, (
        "a successful write must advance the etag"
    )

    # Writer B still holds the now-stale etag.
    with pytest.raises(DavPreconditionFailed):
        await nc_client.calendar.update_event(
            calendar_name, uid, {"title": "writer B"}, shared_etag
        )

    # Writer A's content survived.
    event, _ = await nc_client.calendar.get_event(calendar_name, uid)
    assert event["title"] == "writer A"


async def test_write_with_the_current_etag_succeeds(
    nc_client: NextcloudClient, etag_test_event
):
    """Chaining updates works without re-reading: each write returns the next etag."""
    calendar_name, created = etag_test_event
    uid = created["uid"]

    first = await nc_client.calendar.update_event(
        calendar_name, uid, {"title": "v2"}, created["etag"]
    )
    second = await nc_client.calendar.update_event(
        calendar_name, uid, {"title": "v3"}, first["etag"]
    )

    assert second["etag"] != first["etag"]

    event, _ = await nc_client.calendar.get_event(calendar_name, uid)
    assert event["title"] == "v3"


async def test_unfiltered_event_listing_request_count_does_not_scale(
    nc_client: NextcloudClient, etag_test_event
):
    """The no-date-range branch must not scale requests with the event count.

    ``get_calendar_events`` without a range falls back to caldav's ``events()``,
    which (verified) leaves ``.etag`` unset on every object it returns -- so this
    path needed the same treatment as the date-range REPORT, via one batched
    collection PROPFIND rather than a PROPFIND per event.
    """
    calendar_name, _ = etag_test_event

    calls: list[str] = []
    dav_client = nc_client.calendar._dav_client
    original = dav_client.request

    async def counting_request(url, method="GET", body="", headers=None, **kwargs):
        calls.append(method)
        return await original(url, method, body, headers or {}, **kwargs)

    dav_client.request = counting_request
    try:
        events = await nc_client.calendar.get_calendar_events(calendar_name)
    finally:
        dav_client.request = original

    assert events, "fixture event should be listed"
    assert len(calls) <= 2, (
        f"listing {len(events)} events took {len(calls)} requests ({calls}) -- "
        "the cost must not scale with the number of events"
    )
    assert all(event.get("etag") for event in events), (
        "every listed event needs an etag to be updatable safely"
    )


async def test_date_range_listing_costs_one_request(
    nc_client: NextcloudClient, etag_test_event
):
    """Surfacing ETags must not turn one REPORT into 1 + N requests.

    The date-range REPORT is the hot path for "list events in a range". Objects
    it returns are already loaded, so if the REPORT does not ask for getetag,
    every event falls through to an individual PROPFIND to find one. Measured
    against a live instance: 1 REPORT with the getetag prop, versus 1 + N
    without it.
    """
    calendar_name, created = etag_test_event
    start = datetime.now() - timedelta(days=1)
    end = datetime.now() + timedelta(days=3)

    calls: list[str] = []
    dav_client = nc_client.calendar._dav_client
    original = dav_client.request

    async def counting_request(url, method="GET", body="", headers=None, **kwargs):
        calls.append(method)
        return await original(url, method, body, headers or {}, **kwargs)

    dav_client.request = counting_request
    try:
        events = await nc_client.calendar.get_calendar_events(
            calendar_name, start_datetime=start, end_datetime=end
        )
    finally:
        dav_client.request = original

    assert events, "fixture event should fall inside the range"
    assert calls == ["REPORT"], f"expected a single REPORT, got {calls}"
    assert all(event.get("etag") for event in events), (
        "the REPORT must carry an etag for every event"
    )


@pytest.mark.parametrize("todo_count", [2, 5])
async def test_todo_listing_request_count_does_not_scale(
    nc_client: NextcloudClient, temporary_calendar: str, todo_count: int
):
    """Listing todos must cost a fixed number of requests, not one per todo.

    caldav's ``todos()`` REPORT asks for calendar-data only, so surfacing an
    ETag per todo originally sent ``_object_etag`` to a PROPFIND *each* --
    measured at 1 + N on a live instance. Its ``props`` argument cannot carry
    getetag, so the ETags come from one batched collection PROPFIND instead.

    Parametrized over two sizes precisely because a fixed-count assertion is
    only meaningful if it holds as N changes.
    """
    for i in range(todo_count):
        await nc_client.calendar.create_todo(
            temporary_calendar, {"summary": f"etag listing probe {i}"}
        )

    calls: list[str] = []
    dav_client = nc_client.calendar._dav_client
    original = dav_client.request

    async def counting_request(url, method="GET", body="", headers=None, **kwargs):
        calls.append(method)
        return await original(url, method, body, headers or {}, **kwargs)

    dav_client.request = counting_request
    try:
        todos = await nc_client.calendar.list_todos(temporary_calendar)
    finally:
        dav_client.request = original

    assert len(todos) >= todo_count
    assert len(calls) <= 2, (
        f"listing {len(todos)} todos took {len(calls)} requests ({calls}) -- "
        "the cost must not scale with the number of todos"
    )
    assert all(todo.get("etag") for todo in todos), (
        "every listed todo needs an etag to be updatable safely"
    )
