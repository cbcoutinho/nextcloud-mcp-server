"""Unit tests for shared boundary validators."""

from datetime import datetime, timezone

import pytest

from nextcloud_mcp_server.utils.validation import (
    is_safe_webdav_file_path,
    is_valid_nextcloud_doc_id,
    parse_modified_timestamp,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "1",
        "42",
        "1234567890",
        "9999999999999999999",
    ],
)
def test_accepts_positive_ascii_integers(value):
    """Any positive ASCII integer (no leading zero) is a valid doc_id."""
    assert is_valid_nextcloud_doc_id(value) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,reason",
    [
        ("", "empty string"),
        ("0", "MySQL AUTO_INCREMENT starts at 1"),
        ("01", "leading zero"),
        ("00", "leading zeros"),
        ("-1", "negative"),
        ("+1", "explicit sign"),
        ("1.0", "float-like"),
        (" 1", "leading whitespace"),
        ("1 ", "trailing whitespace"),
        ("1\n", "trailing newline"),
        ("abc", "alphabetic"),
        ("1a", "trailing letter"),
        ("a1", "leading letter"),
        # Unicode digit classes that pass str.isdigit() but are not ASCII.
        # `²` (U+00B2) is a superscript and would slip past the old guard.
        ("²", "Unicode superscript-2"),
        # `٢` (U+0662) Arabic-Indic digit two — passes both isdigit() and
        # isdecimal(), so only an explicit ASCII regex catches it.
        ("٢", "Arabic-Indic digit two"),
        # `१` (U+0967) Devanagari digit one — same story.
        ("१", "Devanagari digit one"),
        # Mixed ASCII + Unicode digits.
        ("1٢", "mixed ASCII + Arabic-Indic"),
    ],
)
def test_rejects_invalid_doc_ids(value, reason):
    """Reject empty/zero/leading-zero/non-ASCII/non-digit inputs."""
    assert is_valid_nextcloud_doc_id(value) is False, f"should reject: {reason}"


# is_safe_webdav_file_path --------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "/Documents/report.pdf",
        "/Documents/My%20File.pdf",
        "/Documents/archive..2026.pdf",
    ],
)
def test_safe_webdav_file_path_accepts_normal_paths(value):
    assert is_safe_webdav_file_path(value) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "/Documents/../../../etc/passwd",
        "/../secret.pdf",
        "/folder/..%2F..%2Fetc/passwd",
        "/folder/%252e%252e%252Fsecret.pdf",
        "/folder/%2e%2e%5Csecret.pdf",
        "/folder/%00secret.pdf",
    ],
)
def test_safe_webdav_file_path_rejects_traversal(value):
    assert is_safe_webdav_file_path(value) is False


# parse_modified_timestamp (ADR-027) ---------------------------------------

_JAN_2026_UTC = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        # RFC 3339 / ISO 8601
        ("2026-01-01T00:00:00Z", _JAN_2026_UTC),
        ("2026-01-01T00:00:00+00:00", _JAN_2026_UTC),
        # +02:00 offset is two hours earlier in UTC
        ("2026-01-01T02:00:00+02:00", _JAN_2026_UTC),
        # Naive datetime is assumed UTC
        ("2026-01-01T00:00:00", _JAN_2026_UTC),
        # Date-only ISO form
        ("2026-01-01", _JAN_2026_UTC),
        # Bare Unix seconds (string and int) pass through
        (str(_JAN_2026_UTC), _JAN_2026_UTC),
        (_JAN_2026_UTC, _JAN_2026_UTC),
        (0, 0),
        (1767225600.9, 1767225600),  # float truncates
    ],
)
def test_parse_modified_timestamp_accepts(value, expected):
    """RFC 3339 strings, Unix seconds, and None normalize to int seconds (UTC)."""
    assert parse_modified_timestamp(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,reason",
    [
        ("not-a-date", "unparseable string"),
        ("2026-13-01T00:00:00Z", "invalid month"),
        (-1, "negative int"),
        (-5.0, "negative float"),
        (True, "bool is not a timestamp"),
        (False, "bool is not a timestamp"),
        ([], "wrong type"),
    ],
)
def test_parse_modified_timestamp_rejects(value, reason):
    """Bad formats / negatives / bools raise ValueError (→ McpError or HTTP 400)."""
    with pytest.raises(ValueError):
        parse_modified_timestamp(value)


@pytest.mark.unit
def test_parse_modified_timestamp_error_names_param():
    """The param_name is surfaced in the error for caller-friendly messages."""
    with pytest.raises(ValueError, match="modified_after"):
        parse_modified_timestamp("nope", param_name="modified_after")
