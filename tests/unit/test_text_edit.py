"""Unit tests for the exact-match edit and line-window helpers (utils/text_edit)."""

import io

import pytest

from nextcloud_mcp_server.utils.text_edit import (
    EditError,
    LineRangeError,
    TextEdit,
    apply_edits,
    line_window,
    unified_diff,
)

# ── apply_edits ─────────────────────────────────────────────────────────


def test_replaces_a_unique_passage():
    result = apply_edits("one\ntwo\nthree\n", [TextEdit("two", "2")])

    assert result.text == "one\n2\nthree\n"
    assert result.replacements == 1


def test_empty_new_string_deletes():
    result = apply_edits("keep\ndrop me\nkeep\n", [TextEdit("drop me\n", "")])

    assert result.text == "keep\nkeep\n"


def test_missing_passage_is_an_error():
    """The caller must learn that nothing matched, not get a silent no-op."""
    with pytest.raises(EditError, match="not found"):
        apply_edits("one\ntwo\n", [TextEdit("three", "3")])


def test_ambiguous_passage_names_the_lines():
    """Several matches without replace_all: say where, so the caller can widen it."""
    with pytest.raises(EditError, match=r"occurs 2 times \(starting on lines 1, 3\)"):
        apply_edits("x = 1\ny\nx = 1\n", [TextEdit("x = 1", "x = 2")])


def test_ambiguous_error_caps_the_line_list():
    text = "hit\n" * 15
    with pytest.raises(EditError, match="and 5 more"):
        apply_edits(text, [TextEdit("hit", "miss")])


def test_replace_all_replaces_every_occurrence():
    result = apply_edits("a-b-a-b\n", [TextEdit("a", "c", replace_all=True)])

    assert result.text == "c-b-c-b\n"
    assert result.replacements == 2


def test_edits_apply_in_order_to_the_previous_result():
    """The second edit sees the first one's output, like a sequence of edits."""
    result = apply_edits(
        "alpha\n", [TextEdit("alpha", "beta"), TextEdit("beta", "gamma")]
    )

    assert result.text == "gamma\n"
    assert result.replacements == 2


def test_a_failing_later_edit_is_reported_with_its_position():
    with pytest.raises(EditError, match="Edit 2 of 2.*after the previous edits"):
        apply_edits("alpha\n", [TextEdit("alpha", "beta"), TextEdit("alpha", "x")])


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (TextEdit("", "x"), "old_string is empty"),
        (TextEdit("same", "same"), "identical"),
    ],
)
def test_degenerate_edits_are_errors(edit, expected):
    with pytest.raises(EditError, match=expected):
        apply_edits("same\n", [edit])


def test_no_edits_is_an_error():
    with pytest.raises(EditError, match="No edits"):
        apply_edits("text", [])


def test_crlf_files_keep_their_line_endings():
    """Models quote with \\n. In a CRLF file the match and the result use CRLF."""
    result = apply_edits("a\r\nb\r\nc\r\n", [TextEdit("a\nb", "x\ny")])

    assert result.text == "x\r\ny\r\nc\r\n"


def test_crlf_mismatch_hint_in_not_found_error():
    with pytest.raises(EditError, match="CRLF"):
        apply_edits("a\r\nb\r\n", [TextEdit("missing", "x")])


def test_byte_order_mark_survives_an_edit():
    result = apply_edits("﻿hello\n", [TextEdit("hello", "bye")])

    assert result.text == "﻿bye\n"


# ── unified_diff ────────────────────────────────────────────────────────


def test_diff_shows_the_changed_line():
    diff, truncated = unified_diff("a\nb\nc\n", "a\nB\nc\n", "notes.md")

    assert "-b" in diff.splitlines()
    assert "+B" in diff.splitlines()
    assert truncated is False


def test_diff_is_truncated_for_sweeping_changes():
    before = "".join(f"line {i}\n" for i in range(500))
    after = before.replace("line", "LINE")

    diff, truncated = unified_diff(before, after, "big.txt", max_lines=50)

    assert truncated is True
    assert len(diff.splitlines()) == 50


# ── line_window ─────────────────────────────────────────────────────────


def _lines(text: bytes) -> io.BytesIO:
    return io.BytesIO(text)


def test_window_is_byte_exact_without_numbers():
    window = line_window(_lines(b"one\r\ntwo\r\nthree\r\n"), 2, 3)

    assert window.content == "two\r\nthree\r\n"
    assert (window.first, window.last, window.total) == (2, 3, 3)


def test_window_with_numbers():
    window = line_window(_lines(b"one\ntwo\nthree\n"), 2, None, numbered=True)

    assert window.content == "     2\ttwo\n     3\tthree"


def test_window_end_past_the_file_is_clamped():
    window = line_window(_lines(b"one\ntwo"), 1, 99)

    assert window.content == "one\ntwo"
    assert (window.last, window.total) == (2, 2)


def test_window_start_past_the_end_is_an_error():
    with pytest.raises(LineRangeError, match="has 2 lines"):
        line_window(_lines(b"one\ntwo\n"), 3, None)


def test_empty_file_reads_as_empty():
    window = line_window(_lines(b""), 1, None)

    assert window.content == ""
    assert window.total == 0


def test_window_is_capped_by_bytes():
    window = line_window(_lines(b"aaaa\nbbbb\ncccc\n"), 1, None, max_bytes=10)

    assert window.content == "aaaa\nbbbb\n"
    assert window.truncated is True
    assert (window.last, window.total) == (2, 3)


def test_only_the_window_is_decoded():
    """A binary tail outside the window does not spoil reading the text head."""
    window = line_window(_lines(b"text\n\xff\xfe\n"), 1, 1)

    assert window.content == "text\n"


def test_window_that_is_not_utf8_raises():
    with pytest.raises(UnicodeDecodeError):
        line_window(_lines(b"text\n\xff\xfe\n"), 2, 2)
