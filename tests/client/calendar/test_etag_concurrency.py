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
