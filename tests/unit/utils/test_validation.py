"""Unit tests for shared boundary validators."""

import pytest

from nextcloud_mcp_server.utils.validation import is_valid_nextcloud_doc_id


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
