"""Unit tests for writing VTODO RRULE on create (issue #1658).

The read path already expands recurring todos; create previously ignored
``recurrence_rule`` in todo_data, so MCP callers could never store one.
"""

from __future__ import annotations

import pytest
from icalendar import Calendar as ICalendar

from nextcloud_mcp_server.client.calendar import CalendarClient

pytestmark = pytest.mark.unit


def _pure_client() -> CalendarClient:
    return CalendarClient.__new__(CalendarClient)


def _vtodo(ical: str):
    return ICalendar.from_ical(ical).walk("VTODO")[0]


def _rrule(ical: str) -> str:
    return _vtodo(ical).get("rrule").to_ical().decode()


ALL_DAY_TODO = {
    "summary": "Weekly backup check",
    "dtstart": "2026-02-10",
    "due": "2026-02-10",
}

TIMED_TODO = {
    "summary": "Standup notes",
    "dtstart": "2026-02-10T10:00:00Z",
    "due": "2026-02-10T11:00:00Z",
}


def test_create_todo_writes_rrule_from_recurrence_rule():
    ical = _pure_client()._create_ical_todo(
        {**ALL_DAY_TODO, "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO"},
        "uid-weekly",
    )

    assert _rrule(ical) == "FREQ=WEEKLY;BYDAY=MO"


def test_create_todo_recurrence_end_date_uses_date_until_for_all_day():
    ical = _pure_client()._create_ical_todo(
        {
            **ALL_DAY_TODO,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
            "recurrence_end_date": "2026-06-30",
        },
        "uid-until-allday",
    )

    assert _rrule(ical) == "FREQ=WEEKLY;UNTIL=20260630;BYDAY=MO"


def test_create_todo_recurrence_end_date_uses_utc_until_for_timed():
    ical = _pure_client()._create_ical_todo(
        {
            **TIMED_TODO,
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            "recurrence_end_date": "2026-06-30",
        },
        "uid-until-timed",
    )

    assert _rrule(ical) == "FREQ=WEEKLY;UNTIL=20260630T235959Z;BYDAY=TU"


def test_create_todo_recurring_false_suppresses_the_rule():
    ical = _pure_client()._create_ical_todo(
        {
            **ALL_DAY_TODO,
            "recurrence_rule": "FREQ=DAILY",
            "recurring": False,
        },
        "uid-suppressed",
    )

    assert "rrule" not in _vtodo(ical)


def test_create_todo_without_recurrence_rule_has_no_rrule():
    ical = _pure_client()._create_ical_todo(ALL_DAY_TODO, "uid-plain")

    assert "rrule" not in _vtodo(ical)


@pytest.mark.parametrize("bound", ["COUNT=5", "UNTIL=20260101T000000Z"])
def test_create_todo_rejects_end_date_when_rule_already_bounds_itself(bound):
    todo_data = {
        **TIMED_TODO,
        "recurrence_rule": f"FREQ=DAILY;{bound}",
        "recurrence_end_date": "2026-06-30",
    }

    with pytest.raises(ValueError, match="conflicts with"):
        _pure_client()._create_ical_todo(todo_data, "uid-conflict")
