"""Unit tests for shared boundary validators."""

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.utils.validation import (
    is_valid_nextcloud_doc_id,
    parse_modified_timestamp,
)

pytestmark = pytest.mark.unit


@given(st.integers(min_value=1))
def test_accepts_every_positive_integer(n):
    assert is_valid_nextcloud_doc_id(str(n)) is True


@given(st.text())
@example("0")  # MySQL AUTO_INCREMENT starts at 1
@example("01")  # leading zero
@example("+1")
@example("1\n")
@example("²")  # superscript: passes str.isdigit()
@example("٢")  # Arabic-Indic: passes str.isdecimal() too
@example("1٢")
def test_accepts_only_the_canonical_ascii_form(value):
    """Valid iff the string is exactly how a positive int prints."""
    canonical = value.isascii() and value.isdigit() and str(int(value)) == value
    assert is_valid_nextcloud_doc_id(value) is (canonical and int(value) > 0)


# parse_modified_timestamp (ADR-027) ---------------------------------------

_offsets = st.integers(min_value=-23 * 60, max_value=23 * 60).map(
    lambda m: timezone(timedelta(minutes=m))
)
_instants = st.datetimes(
    min_value=datetime(1970, 1, 2), max_value=datetime(9998, 12, 31), timezones=_offsets
).map(lambda dt: dt.replace(microsecond=0))


@given(_instants)
@example(datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))))
def test_rfc3339_round_trips_to_unix_seconds(instant):
    assert parse_modified_timestamp(instant.isoformat()) == int(instant.timestamp())


@given(_instants)
def test_z_suffix_and_naive_mean_utc(instant):
    utc = instant.astimezone(timezone.utc).replace(tzinfo=None)
    expected = int(instant.timestamp())
    assert parse_modified_timestamp(utc.isoformat() + "Z") == expected
    assert parse_modified_timestamp(utc.isoformat()) == expected


@given(st.integers(min_value=0, max_value=2**53))
def test_unix_seconds_pass_through_as_int_or_str(seconds):
    assert parse_modified_timestamp(seconds) == seconds
    assert parse_modified_timestamp(str(seconds)) == seconds
    if seconds < 2**40:  # beyond that, +0.9 is not representable
        assert parse_modified_timestamp(seconds + 0.9) == seconds


_anything = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(),
    st.text(),
    st.lists(st.integers(), max_size=2),
)


@given(_anything)
@example("   ")
@example("not-a-date")
@example("2026-13-01T00:00:00Z")
@example(-1)
@example(-5.0)
@example(True)
@example(float("inf"))  # json.loads accepts Infinity
@example(float("nan"))
@example("1969-12-31T00:00:00Z")  # before the epoch: negative like -1
def test_result_is_an_open_bound_or_non_negative_seconds(value):
    """Request input: anything other than ValueError would be a 500, not a 400."""
    try:
        result = parse_modified_timestamp(value)
    except ValueError:
        return
    assert result is None or (type(result) is int and result >= 0)


def test_parse_modified_timestamp_error_names_param():
    """The param_name is surfaced in the error for caller-friendly messages."""
    with pytest.raises(ValueError, match="modified_after"):
        parse_modified_timestamp("nope", param_name="modified_after")
