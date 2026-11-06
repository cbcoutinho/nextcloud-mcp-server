"""A calendar filter that cannot evaluate an item keeps it, and says so.

Both filters fail open, so an unreadable event or todo stays in the result
rather than vanishing. Before, that fallback was silent, so a bug in any filter
clause quietly matched everything. These pin that the fallback is still taken
and that it is logged with the traceback.
"""

import logging

import pytest

from nextcloud_mcp_server.client.calendar import CalendarClient

pytestmark = pytest.mark.unit

LOGGER = "nextcloud_mcp_server.client.calendar"


@pytest.fixture
def client() -> CalendarClient:
    return CalendarClient.__new__(CalendarClient)


def test_unreadable_event_is_kept_and_logged(client, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    event = {"uid": "ev-1", "start_datetime": "not a date"}

    assert client._event_matches_filters(event, {"min_duration_minutes": 30})

    (record,) = caplog.records
    assert "ev-1" in record.getMessage()
    assert record.exc_info is not None


def test_unreadable_todo_is_kept_and_logged(client, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    todo = {"uid": "todo-1", "status": None}  # None.upper() raises

    assert client._todo_matches_filters(todo, {"status": "COMPLETED"})

    (record,) = caplog.records
    assert "todo-1" in record.getMessage()
    assert record.exc_info is not None
