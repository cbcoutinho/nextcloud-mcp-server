"""Unit tests for CalDAV ETag concurrency control.

CalDAV writes used to send no ``If-Match`` and to hand callers a stubbed empty
ETag, so two concurrent updates silently last-write-win and callers had no
token to do anything about it. These pin the two halves of the fix: reads
surface the real ETag, and writes are conditional on it.

``caldav``'s ``put`` *returns* a 412 rather than raising, so the status check in
``_conditional_put`` is the thing standing between a caller and a silently
clobbered event.
"""

import pytest
from caldav.elements import dav

from nextcloud_mcp_server.client.calendar import CalendarClient
from nextcloud_mcp_server.client.dav_errors import DavPreconditionFailed

pytestmark = pytest.mark.unit

ETAG = '"0b406c692bc303debec7507ef061be26"'
NEW_ETAG = '"6bc8c14c8ab4cc42ef7e537c092b4d8f"'

PRECONDITION_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:error xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">
  <s:exception>Sabre\\DAV\\Exception\\PreconditionFailed</s:exception>
  <s:message>An If-Match header was specified, but none of the specified ETags matched.</s:message>
</d:error>"""


@pytest.fixture
def client(mocker):
    """CalendarClient with its DAV client mocked out."""
    mocker.patch("nextcloud_mcp_server.client.calendar.AsyncDAVClient")
    return CalendarClient("https://cloud.example.org", "alice", password="pw")


def _put_response(mocker, status: int, etag: str | None = None, raw: bytes = b""):
    response = mocker.Mock()
    response.status = status
    response.headers = {"etag": etag} if etag else {}
    response.raw = raw
    return response


async def test_object_etag_prefers_the_loaded_property(mocker):
    """``load()`` already populates getetag, so the common path costs no request."""
    obj = mocker.Mock()
    obj.etag = ETAG
    obj.get_properties = mocker.AsyncMock()

    assert await CalendarClient._object_etag(obj) == ETAG
    obj.get_properties.assert_not_called()


async def test_object_etag_falls_back_to_propfind(mocker):
    """An object reached without a load still yields an ETag."""
    obj = mocker.Mock()
    obj.etag = None
    obj.get_properties = mocker.AsyncMock(return_value={dav.GetEtag.tag: ETAG})

    assert await CalendarClient._object_etag(obj) == ETAG
    obj.get_properties.assert_awaited_once()


async def test_object_etag_degrades_when_the_server_withholds_one(mocker):
    """A failed ETag fetch must not fail the read that asked for the object.

    The ETag is an optimisation for the caller. Failing the whole read because
    the concurrency token could not be fetched is the worse trade.
    """
    obj = mocker.Mock()
    obj.etag = None
    obj.get_properties = mocker.AsyncMock(side_effect=RuntimeError("PROPFIND failed"))

    assert await CalendarClient._object_etag(obj) == ""


async def test_collection_etags_degrades_when_the_propfind_fails(client, mocker):
    """The batched fetch degrades the same way the per-object one does.

    Listings call this before iterating, so a raise here would fail the whole
    listing over a concurrency token the caller may not even use.
    """
    calendar = mocker.Mock()
    calendar.url = "https://cloud.example.org/remote.php/dav/calendars/alice/personal/"
    client._dav_client.request = mocker.AsyncMock(
        side_effect=RuntimeError("PROPFIND failed")
    )

    assert await client._collection_etags(calendar) == {}


async def test_collection_etags_maps_decoded_href_to_etag(client, mocker):
    """Keys are decoded paths, matching how callers look objects up."""
    calendar = mocker.Mock()
    calendar.url = "https://cloud.example.org/remote.php/dav/calendars/alice/personal/"
    response = mocker.Mock()
    response.expand_simple_props.return_value = {
        # Percent-encoded on the wire; callers hold the decoded path.
        "/remote.php/dav/calendars/alice/personal/my%20event.ics": {
            dav.GetEtag.tag: ETAG
        },
        # An object the server reports without an etag is simply absent.
        "/remote.php/dav/calendars/alice/personal/other.ics": {},
    }
    client._dav_client.request = mocker.AsyncMock(return_value=response)

    assert await client._collection_etags(calendar) == {
        "/remote.php/dav/calendars/alice/personal/my event.ics": ETAG
    }


async def test_conditional_put_sends_if_match_and_returns_the_new_etag(client, mocker):
    obj = mocker.Mock()
    obj.url = "https://cloud.example.org/remote.php/dav/calendars/alice/personal/e.ics"
    client._dav_client.put = mocker.AsyncMock(
        return_value=_put_response(mocker, 204, NEW_ETAG)
    )

    result = await client._conditional_put(
        obj, "BEGIN:VCALENDAR", ETAG, kind="event", uid="e"
    )

    assert result == NEW_ETAG
    headers = client._dav_client.put.call_args.args[2]
    assert headers["If-Match"] == ETAG


async def test_conditional_put_omits_if_match_without_an_etag(client, mocker):
    """No ETag means no precondition to assert -- an unconditional write."""
    obj = mocker.Mock()
    obj.url = "https://cloud.example.org/remote.php/dav/calendars/alice/personal/e.ics"
    client._dav_client.put = mocker.AsyncMock(
        return_value=_put_response(mocker, 204, NEW_ETAG)
    )

    await client._conditional_put(obj, "BEGIN:VCALENDAR", "", kind="event", uid="e")

    assert "If-Match" not in client._dav_client.put.call_args.args[2]


async def test_conditional_put_raises_typed_error_on_stale_etag(client, mocker):
    """A 412 is *returned* by caldav, so it must be detected, not awaited-on-raise."""
    obj = mocker.Mock()
    obj.url = "https://cloud.example.org/remote.php/dav/calendars/alice/personal/e.ics"
    client._dav_client.put = mocker.AsyncMock(
        return_value=_put_response(mocker, 412, raw=PRECONDITION_BODY)
    )

    with pytest.raises(DavPreconditionFailed) as exc_info:
        await client._conditional_put(
            obj, "BEGIN:VCALENDAR", ETAG, kind="event", uid="e"
        )

    # The server's own explanation survives the trip through caldav.
    assert exc_info.value.dav_exception == "Sabre\\DAV\\Exception\\PreconditionFailed"
    assert "none of the specified ETags matched" in str(exc_info.value)


async def test_conditional_put_raises_on_other_refusals(client, mocker):
    """A non-412 failure still raises rather than being reported as success."""
    obj = mocker.Mock()
    obj.url = "https://cloud.example.org/remote.php/dav/calendars/alice/personal/e.ics"
    client._dav_client.put = mocker.AsyncMock(return_value=_put_response(mocker, 403))

    with pytest.raises(Exception) as exc_info:
        await client._conditional_put(
            obj, "BEGIN:VCALENDAR", ETAG, kind="event", uid="e"
        )

    assert exc_info.value.response.status_code == 403
