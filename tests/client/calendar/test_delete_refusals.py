"""Unit tests for structured CalDAV delete-refusal handling.

``delete_event`` / ``delete_todo`` used to catch only ``NotFoundError``, so a
server *refusal* — a 403 on a scheduled (iMIP) object, or a 409/412 from a stale
entry in the calendar trashbin still holding the UID — escaped as a raw caldav
traceback out of the MCP tool.

The shape being pinned here was verified against the installed caldav:
``DAVObject._post_delete`` raises ``DeleteError(errmsg(r))`` where ``errmsg``
formats ``"<status> <reason>\\n\\n<body>"``, and ``DAVError.__init__``'s first
positional parameter is ``url`` — so the status string lands in ``exc.url``, not
``exc.reason``.
"""

from __future__ import annotations

import httpx
import pytest
from caldav.lib import error as caldav_error

from nextcloud_mcp_server.client.calendar import CalendarClient

pytestmark = pytest.mark.unit


def _client(mocker, *, delete_raises: Exception | None = None) -> CalendarClient:
    """Build a CalendarClient whose object lookup and delete are stubbed."""
    client = CalendarClient.__new__(CalendarClient)
    client._client = mocker.AsyncMock(spec=httpx.AsyncClient)
    client._principal_resolved = True
    mocker.patch.object(client, "_ensure_calendar_home", mocker.AsyncMock())
    mocker.patch.object(
        client, "_get_calendar", mocker.Mock(return_value=mocker.Mock())
    )

    obj = mocker.Mock()
    obj.delete = (
        mocker.Mock(side_effect=delete_raises) if delete_raises else mocker.Mock()
    )
    mocker.patch.object(
        client, "_async_object_by_uid", mocker.AsyncMock(return_value=obj)
    )
    return client


def _delete_error(status_line: str) -> caldav_error.DeleteError:
    """Build a DeleteError exactly as caldav's _post_delete does."""
    return caldav_error.DeleteError(f"{status_line}\n\n<d:error/>")


@pytest.mark.parametrize("method,uid", [("delete_event", "e1"), ("delete_todo", "t1")])
async def test_successful_delete_reports_204(mocker, method, uid):
    client = _client(mocker)
    result = await getattr(client, method)("Personal", uid)
    assert result == {"success": True, "status_code": 204}


@pytest.mark.parametrize("method,uid", [("delete_event", "e1"), ("delete_todo", "t1")])
async def test_missing_object_stays_idempotent_404(mocker, method, uid):
    """A missing object is a *success*: retrying a delete must be safe.

    ``NotFoundError`` and ``DeleteError`` are flat siblings under ``DAVError``,
    so clause ordering is not load-bearing here — but widening the refusal clause
    to ``DAVError`` would swallow this and report a refusal instead.
    """
    client = _client(mocker)
    mocker.patch.object(
        client,
        "_async_object_by_uid",
        mocker.AsyncMock(side_effect=caldav_error.NotFoundError("nope")),
    )

    result = await getattr(client, method)("Personal", uid)

    assert result == {"success": True, "status_code": 404}


@pytest.mark.parametrize("method,uid", [("delete_event", "e1"), ("delete_todo", "t1")])
async def test_403_refusal_names_scheduled_objects(mocker, method, uid):
    client = _client(mocker, delete_raises=_delete_error("403 Forbidden"))

    result = await getattr(client, method)("Personal", uid)

    assert result["success"] is False
    assert result["status_code"] == 403
    assert "scheduled" in result["message"]
    assert "403 Forbidden" in result["reason"]


@pytest.mark.parametrize("status", ["409 Conflict", "412 Precondition Failed"])
async def test_conflict_refusal_names_the_trashbin(mocker, status):
    client = _client(mocker, delete_raises=_delete_error(status))

    result = await client.delete_event("Personal", "e1")

    assert result["success"] is False
    assert result["status_code"] == int(status.split()[0])
    assert "trashbin" in result["message"]


async def test_unparseable_refusal_is_not_guessed(mocker):
    """An unknown refusal must surface as 500, not a plausible-looking code."""
    client = _client(mocker, delete_raises=caldav_error.DeleteError())

    result = await client.delete_event("Personal", "e1")

    assert result["success"] is False
    assert result["status_code"] == 500
    assert "refused" in result["message"]


async def test_transport_errors_still_propagate(mocker):
    """Refusals are handled; connectivity failures are not ours to swallow."""
    client = _client(mocker, delete_raises=httpx.ConnectError("boom"))
    pending = client.delete_event("Personal", "e1")

    with pytest.raises(httpx.ConnectError):
        await pending


@pytest.mark.parametrize(
    "exc",
    [caldav_error.AuthorizationError(), caldav_error.RateLimitError()],
    ids=["authorization", "rate_limit"],
)
async def test_non_refusal_dav_errors_still_propagate(mocker, exc):
    """Only DeleteError is a refusal.

    AuthorizationError (expired credential) and RateLimitError (retryable) are
    siblings under DAVError, not delete rejections. Catching the DAVError base
    would flatten an auth failure into a per-object "the server refused this
    event" message and hide a systemic problem — this test is what caught that
    in the first implementation.
    """
    client = _client(mocker)
    mocker.patch.object(
        client, "_async_object_by_uid", mocker.AsyncMock(side_effect=exc)
    )
    pending = client.delete_event("Personal", "e1")

    with pytest.raises(type(exc)):
        await pending


@pytest.mark.parametrize(
    "status_line,expected",
    [
        ("403 Forbidden", 403),
        ("409 Conflict", 409),
        ("412 Precondition Failed", 412),
        ("500 Internal Server Error", 500),
    ],
)
def test_status_parsing_from_caldav_error(status_line, expected):
    exc = _delete_error(status_line)
    assert CalendarClient._status_from_dav_error(exc) == expected


def test_status_parsing_returns_none_when_absent():
    assert CalendarClient._status_from_dav_error(caldav_error.DeleteError()) is None
