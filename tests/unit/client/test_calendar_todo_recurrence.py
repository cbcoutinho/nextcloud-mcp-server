"""Unit tests for client-side VTODO recurrence expansion.

CalDAV hands back the whole resource but never expands VTODO recurrences, and
the master component's DTSTART/DUE describe the *first* instance. Without
expansion a monthly chore created in 2024 is reported as due 2024-02-03
forever, which reads as "years overdue" to any consumer.

Clients that materialise recurrences (jtx Board via DAVx5) write one
RECURRENCE-ID override per instance and mark finished ones COMPLETED, so the
unfinished backlog is recoverable from the resource. These tests pin that it is
recovered, and that it survives the Pydantic mapping the server layer performs.
"""

import datetime as dt

import pytest

from nextcloud_mcp_server.client.calendar import CalendarClient
from nextcloud_mcp_server.models.calendar import Todo

pytestmark = pytest.mark.unit

# 2026-07-30: inside the 2026-07-28 window of the monthly series below.
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


def _monthly_series(done_through: tuple[int, int] = (2026, 4)) -> str:
    """A materialised monthly series, finished up to and including ``done_through``.

    Mirrors what jtx Board writes: a master carrying the RRULE plus one
    RECURRENCE-ID override per instance, finished ones marked COMPLETED.
    """
    parts = [
        """
BEGIN:VTODO
UID:monthly
SUMMARY:Geldabrechnung
DTSTART:20240128T130000
DUE:20240203T130000
RRULE:FREQ=MONTHLY;INTERVAL=1;BYMONTHDAY=28
PRIORITY:4
END:VTODO
"""
    ]
    for year in (2024, 2025, 2026):
        for month in range(1, 13):
            if (year, month) > (2026, 7):
                continue
            start = dt.datetime(year, month, 28, 13, 0)
            due = start + dt.timedelta(days=6)
            status = (
                "STATUS:COMPLETED\nPERCENT-COMPLETE:100\nCOMPLETED:20260517T180549Z"
                if (year, month) <= done_through
                else "STATUS:NEEDS-ACTION"
            )
            parts.append(f"""
BEGIN:VTODO
UID:monthly
SUMMARY:Geldabrechnung
DTSTART:{start:%Y%m%dT%H%M%S}
RECURRENCE-ID:{start:%Y%m%dT%H%M%S}
DUE:{due:%Y%m%dT%H%M%S}
PRIORITY:4
{status}
END:VTODO
""")
    return _ical(*parts)


def test_backlog_reports_only_unfinished_started_occurrences():
    """The three open instances are found; the completed ones are not."""
    todo = _client()._parse_ical_todo(_monthly_series(), now=NOW)

    assert todo["pending_count"] == 3
    # Oldest still-open instance — "overdue since".
    assert todo["oldest_pending_dtstart"] == "2026-05-28T13:00:00"
    assert todo["oldest_pending_due"] == "2026-06-03T13:00:00"
    # Newest, i.e. the one currently inside its window.
    assert todo["current_dtstart"] == "2026-07-28T13:00:00"
    assert todo["current_due"] == "2026-08-03T13:00:00"


def test_fully_completed_series_reports_zero_pending():
    """A series whose started occurrences are all done is up to date."""
    todo = _client()._parse_ical_todo(_monthly_series(done_through=(2026, 12)), now=NOW)

    assert todo["pending_count"] == 0
    assert "current_due" not in todo
    assert "oldest_pending_due" not in todo


def test_series_starting_in_the_future_has_no_backlog():
    """Nothing has started yet, so the backlog is empty rather than unresolved.

    The naive query would span from the series start back to now — a window
    ending before it begins, which the expander rejects outright.
    """
    future = """
BEGIN:VTODO
UID:colonoscopy
SUMMARY:Termin Darmspiegelung ausmachen
DTSTART;VALUE=DATE:20261001
DUE;VALUE=DATE:20261127
RRULE:FREQ=YEARLY;COUNT=-1;INTERVAL=5
PRIORITY:3
END:VTODO
"""
    todo = _client()._parse_ical_todo(_ical(future), now=NOW)

    assert todo["pending_count"] == 0
    assert "current_due" not in todo


def test_percent_complete_counts_as_done_without_status():
    """Some clients only set PERCENT-COMPLETE on a finished instance."""
    ics = _ical(
        """
BEGIN:VTODO
UID:weekly
SUMMARY:Kaffeemaschine
DTSTART:20260704T090000
DUE:20260704T170000
RRULE:FREQ=WEEKLY;BYDAY=SA;UNTIL=20260726T090000
PRIORITY:3
END:VTODO
""",
        """
BEGIN:VTODO
UID:weekly
SUMMARY:Kaffeemaschine
DTSTART:20260725T090000
RECURRENCE-ID:20260725T090000
DUE:20260725T170000
PERCENT-COMPLETE:100
END:VTODO
""",
    )
    todo = _client()._parse_ical_todo(ics, now=NOW)

    # 04.07., 11.07. and 18.07. stay open; 25.07. is done via PERCENT-COMPLETE.
    assert todo["pending_count"] == 3
    assert todo["current_dtstart"] == "2026-07-18T09:00:00"


def test_unmaterialised_series_still_reports_the_backlog():
    """Without overrides every started occurrence counts as pending.

    Also pins the three-year lookback: the series starts in 2023 but only the
    2024, 2025 and 2026 instances fall inside the window, so ``pending_count``
    is a lower bound rather than the true backlog depth.
    """
    todo = _client()._parse_ical_todo(_ical(YEARLY_TODO), now=NOW)

    assert todo["pending_count"] == 3
    assert todo["oldest_pending_due"] == "2024-06-15"
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
    assert "pending_count" not in todo
    assert todo["due"] == "2023-06-15"


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
    """An RRULE has no anchor without DTSTART, so nothing can be resolved.
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
    assert "pending_count" not in todo


def test_todo_model_round_trip_preserves_recurrence_fields():
    """Mirrors the server's ``Todo(**todo_data)`` mapping — an unmodelled field
    would be dropped there and never reach the caller."""
    todo_data = _client()._parse_ical_todo(_monthly_series(), now=NOW)

    todo = Todo(**todo_data)

    assert todo.recurring is True
    assert todo.pending_count == 3
    assert todo.oldest_pending_due == "2026-06-03T13:00:00"
    assert todo.current_due == "2026-08-03T13:00:00"
