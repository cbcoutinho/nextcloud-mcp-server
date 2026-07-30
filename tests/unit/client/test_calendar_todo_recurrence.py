"""Unit tests for client-side VTODO recurrence expansion.

CalDAV hands back only the master VTODO of a recurring task, whose DTSTART/DUE
describe the *first* instance. Without expansion a yearly chore created in 2023
is reported as due 2023-06-15 forever, which reads as "years overdue" to any
consumer. These tests pin that the currently relevant occurrence is surfaced
instead, and that it survives the Pydantic mapping the server layer performs.
"""

import datetime as dt

import pytest

from nextcloud_mcp_server.client.calendar import CalendarClient
from nextcloud_mcp_server.models.calendar import Todo

pytestmark = pytest.mark.unit

# 2026-07-30: past the 2026 instance (Jun 1-15) of the yearly series below.
NOW = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.UTC)


def _client() -> CalendarClient:
    """A client for pure-parsing tests; the constructor needs credentials."""
    return CalendarClient.__new__(CalendarClient)


def _ical(*vtodos: str) -> str:
    body = "\n".join(vtodo.strip() for vtodo in vtodos)
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n{body}\nEND:VCALENDAR\n"


YEARLY_TODO = """
BEGIN:VTODO
UID:backup-check
SUMMARY:Check backups
DTSTART;VALUE=DATE:20230601
DUE;VALUE=DATE:20230615
STATUS:NEEDS-ACTION
PRIORITY:2
CATEGORIES:IT
RRULE:FREQ=YEARLY
END:VTODO
"""

PLAIN_TODO = """
BEGIN:VTODO
UID:one-off
SUMMARY:Renew passport
DTSTART;VALUE=DATE:20230601
DUE;VALUE=DATE:20230615
STATUS:NEEDS-ACTION
END:VTODO
"""


def test_recurring_todo_reports_current_occurrence():
    """The 2026 instance is surfaced; the master dates stay untouched."""
    todo = _client()._parse_ical_todo(_ical(YEARLY_TODO), now=NOW)

    assert todo["current_dtstart"] == "2026-06-01"
    assert todo["current_due"] == "2026-06-15"
    # The master keeps addressing the series, so updates still target it.
    assert todo["dtstart"] == "2023-06-01"
    assert todo["due"] == "2023-06-15"
    assert todo["recurring"] is True


def test_recurrence_rule_is_rfc5545_not_python_repr():
    """``str(vRecur)`` would yield ``vRecur({'FREQ': ['YEARLY']})``, which no
    caller can feed back into ``vRecur.from_ical()``."""
    todo = _client()._parse_ical_todo(_ical(YEARLY_TODO), now=NOW)

    assert todo["recurrence_rule"] == "FREQ=YEARLY"


def test_non_recurring_todo_has_no_recurrence_fields():
    todo = _client()._parse_ical_todo(_ical(PLAIN_TODO), now=NOW)

    assert "recurring" not in todo
    assert "current_dtstart" not in todo
    assert todo["due"] == "2023-06-15"


def test_series_not_yet_started_reports_first_occurrence():
    """Before the series begins there is no started occurrence to pick."""
    todo = _client()._parse_ical_todo(
        _ical(YEARLY_TODO), now=dt.datetime(2023, 1, 5, tzinfo=dt.UTC)
    )

    assert todo["current_dtstart"] == "2023-06-01"


def test_recurrence_id_override_does_not_shadow_master():
    """An override stored ahead of the master must not be mistaken for it."""
    override = """
BEGIN:VTODO
UID:backup-check
RECURRENCE-ID;VALUE=DATE:20240601
SUMMARY:Check backups (moved)
DTSTART;VALUE=DATE:20240701
DUE;VALUE=DATE:20240715
STATUS:NEEDS-ACTION
END:VTODO
"""
    todo = _client()._parse_ical_todo(_ical(override, YEARLY_TODO), now=NOW)

    assert todo["summary"] == "Check backups"
    assert todo["recurrence_rule"] == "FREQ=YEARLY"


def test_recurring_todo_without_dtstart_falls_back_to_master_dates():
    """An RRULE has no anchor without DTSTART, so no occurrence can be resolved.
    The todo must still be returned with its stored DUE rather than dropped."""
    anchorless = """
BEGIN:VTODO
UID:no-anchor
SUMMARY:Water the plants
DUE;VALUE=DATE:20230615
STATUS:NEEDS-ACTION
RRULE:FREQ=WEEKLY
END:VTODO
"""
    todo = _client()._parse_ical_todo(_ical(anchorless), now=NOW)

    assert todo is not None
    assert todo["recurring"] is True
    assert todo["due"] == "2023-06-15"
    assert "current_dtstart" not in todo
    assert "current_due" not in todo


def test_todo_model_round_trip_preserves_recurrence_fields():
    """Mirrors the server's ``Todo(**todo_data)`` mapping — an unmodelled field
    would be dropped there and never reach the caller."""
    todo_data = _client()._parse_ical_todo(_ical(YEARLY_TODO), now=NOW)

    todo = Todo(**todo_data)

    assert todo.recurring is True
    assert todo.recurrence_rule == "FREQ=YEARLY"
    assert todo.current_dtstart == "2026-06-01"
    assert todo.current_due == "2026-06-15"
