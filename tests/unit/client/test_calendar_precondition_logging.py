"""A lost ETag race must not be logged as a server error.

``CalendarClient.update_todo`` wraps its body in ``except Exception`` and logs
at ``error`` before re-raising. That fires for ``DavPreconditionFailed`` too --
which is not a failure of the call, but the ``If-Match`` guard doing its job
when another writer got there first.

The tool layer deliberately logs that at ``debug`` ("a caller losing a write
race is the guard working, not a fault"). An ``error`` one layer below
contradicts it, and does so in exactly the concurrent-write scenario someone
would be reading these logs to understand.
"""

from __future__ import annotations

import logging

import pytest
from httpx import Request, Response

from nextcloud_mcp_server.client.calendar import CalendarClient
from nextcloud_mcp_server.client.dav_errors import DavPreconditionFailed

pytestmark = pytest.mark.unit

CALENDAR_LOGGER = "nextcloud_mcp_server.client.calendar"


def _precondition_failed() -> DavPreconditionFailed:
    request = Request("PUT", "https://nc.example.com/dav/todo-1.ics")
    return DavPreconditionFailed(
        "412 Precondition Failed",
        request=request,
        response=Response(412, request=request),
        dav_message="If-Match did not match",
    )


@pytest.fixture
def client(mocker):
    """A CalendarClient with the CalDAV round-trip stubbed out.

    Only the pieces ``update_todo`` touches on its way to the conditional PUT
    are stubbed; the point is the except-ordering after that PUT fails, not the
    DAV plumbing before it.
    """
    calendar_client = CalendarClient(
        "https://nc.example.com", "testuser", password="pw"
    )

    todo = mocker.MagicMock()
    todo.data = "BEGIN:VCALENDAR\nEND:VCALENDAR"
    todo.url = "https://nc.example.com/dav/todo-1.ics"
    todo.load.return_value = None

    mocker.patch.object(
        CalendarClient, "_ensure_calendar_home", mocker.AsyncMock(return_value=None)
    )
    mocker.patch.object(
        CalendarClient, "_get_calendar", return_value=mocker.MagicMock()
    )
    mocker.patch.object(
        CalendarClient, "_async_object_by_uid", mocker.AsyncMock(return_value=todo)
    )
    mocker.patch.object(
        CalendarClient, "_merge_ical_todo_properties", return_value="ICAL"
    )
    return calendar_client


async def test_stale_etag_is_not_logged_as_an_error(client, mocker, caplog):
    """The 412 propagates, and leaves no ERROR record behind."""
    mocker.patch.object(
        CalendarClient,
        "_conditional_put",
        mocker.AsyncMock(side_effect=_precondition_failed()),
    )
    # Logger-scoped, so an unrelated module's ERROR cannot make this pass or
    # fail by accident.
    caplog.set_level(logging.ERROR, logger=CALENDAR_LOGGER)

    with pytest.raises(DavPreconditionFailed):
        await client.update_todo("Personal", "todo-1", {"summary": "x"}, '"stale"')

    errors = [r for r in caplog.records if r.name == CALENDAR_LOGGER]
    assert not errors, [r.getMessage() for r in errors]


async def test_a_real_failure_is_still_logged_as_an_error(client, mocker, caplog):
    """Guard the guard: the catch-all must still fire for everything else.

    Without this, deleting the ``logger.error`` outright would pass the test
    above -- and the error path this narrowing was careful to preserve would be
    silently gone.
    """
    mocker.patch.object(
        CalendarClient,
        "_conditional_put",
        mocker.AsyncMock(side_effect=RuntimeError("caldav exploded")),
    )
    caplog.set_level(logging.ERROR, logger=CALENDAR_LOGGER)

    with pytest.raises(RuntimeError):
        await client.update_todo("Personal", "todo-1", {"summary": "x"}, '"etag"')

    errors = [r for r in caplog.records if r.name == CALENDAR_LOGGER]
    assert len(errors) == 1
    assert "todo-1" in errors[0].getMessage()
