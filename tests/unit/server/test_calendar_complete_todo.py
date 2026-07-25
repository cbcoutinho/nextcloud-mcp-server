"""Unit tests for the VTODO completion payload.

RFC 5545 treats STATUS, PERCENT-COMPLETE and COMPLETED as independent
properties, and ``_merge_ical_todo_properties`` gates each on its own key. So
``nc_calendar_update_todo(status="COMPLETED")`` alone leaves PERCENT-COMPLETE
stale and writes no completion timestamp — clients that surface progress or
completion dates then disagree about whether the task is done.
"""

from __future__ import annotations

import datetime as dt

import pytest

from nextcloud_mcp_server.server.calendar import _completion_payload

pytestmark = pytest.mark.unit


def test_payload_sets_all_three_properties():
    payload = _completion_payload()

    assert payload["status"] == "COMPLETED"
    assert payload["percent_complete"] == 100
    assert payload["completed"]


def test_default_timestamp_is_timezone_aware_utc():
    parsed = dt.datetime.fromisoformat(_completion_payload()["completed"])

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == dt.timedelta(0)


def test_default_timestamp_has_no_microseconds():
    """Keeps the written COMPLETED value readable and stable across clients."""
    assert "." not in _completion_payload()["completed"]


def test_explicit_timestamp_passes_through_verbatim():
    payload = _completion_payload("2026-01-01T00:00:00+00:00")

    assert payload["completed"] == "2026-01-01T00:00:00+00:00"
    # The other two are still set — the caller supplying a timestamp must not
    # have to also remember status and percent_complete.
    assert payload["status"] == "COMPLETED"
    assert payload["percent_complete"] == 100


def test_empty_string_timestamp_falls_back_to_now():
    """An empty string is not a usable COMPLETED value."""
    payload = _completion_payload("")

    assert payload["completed"]
    assert dt.datetime.fromisoformat(payload["completed"]).tzinfo is not None
