"""Unit tests for the exact-match edit and line-window helpers (utils/text_edit)."""

import io

import pytest

from nextcloud_mcp_server.utils.text_edit import (
    EditError,
    LineRangeError,
    TextEdit,
    apply_edits,
    is_editable_text,
    line_window,
    normalize_content_type,
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


def test_pure_crlf_file_matches_single_line_and_replace_all_edits():
    """Without a line break in the edit there is nothing to convert, and
    replace_all counts the CRLF-form matches."""
    text = "a\r\nb\r\na\r\nb\r\n"

    single = apply_edits(text, [TextEdit("b", "B", replace_all=True)])
    multi = apply_edits(text, [TextEdit("a\nb", "x\ny", replace_all=True)])

    assert single.text == "a\r\nB\r\na\r\nB\r\n"
    assert single.replacements == 2
    assert multi.text == "x\r\ny\r\nx\r\ny\r\n"
    assert multi.replacements == 2


def test_lines_added_to_a_pure_crlf_file_get_crlf():
    """old_string has no line break, so it matches as given, but the lines the
    edit adds must not bring bare LFs into a CRLF-only file."""
    text = "title\r\nbody\r\n"

    result = apply_edits(text, [TextEdit("title", "title\nsubtitle")])

    assert result.text == "title\r\nsubtitle\r\nbody\r\n"


def test_lines_added_to_a_mixed_file_are_written_as_given():
    """A file that already mixes LF and CRLF has no single convention to keep."""
    text = "first\nsecond\r\n"

    result = apply_edits(text, [TextEdit("first", "first\nmore")])

    assert result.text == "first\nmore\nsecond\r\n"


def test_lf_region_of_a_mixed_file_is_edited_as_given():
    """The file has CRLF somewhere, but this passage is LF: no conversion."""
    text = "lf one\nlf two\ncrlf one\r\ncrlf two\r\n"

    result = apply_edits(text, [TextEdit("lf one\nlf two", "LF 1\nLF 2")])

    assert result.text == "LF 1\nLF 2\ncrlf one\r\ncrlf two\r\n"


def test_crlf_region_of_a_mixed_file_falls_back_to_crlf():
    """Quoted with \\n, found only with CRLF: matched and written with CRLF."""
    text = "lf one\nlf two\ncrlf one\r\ncrlf two\r\n"

    result = apply_edits(text, [TextEdit("crlf one\ncrlf two", "CRLF 1\nCRLF 2")])

    assert result.text == "lf one\nlf two\nCRLF 1\r\nCRLF 2\r\n"


def test_mixed_file_edits_can_hit_both_regions_in_one_call():
    text = "lf one\nlf two\ncrlf one\r\ncrlf two\r\n"

    result = apply_edits(
        text,
        [
            TextEdit("lf one\nlf two", "A\nB"),
            TextEdit("crlf one\ncrlf two", "C\nD"),
        ],
    )

    assert result.text == "A\nB\nC\r\nD\r\n"
    assert result.replacements == 2


def test_the_form_that_matches_decides_uniqueness_and_replace_all():
    """As given wins, and the two forms are never combined in one edit."""
    text = "x\ny\r\nx\r\ny\r\n"

    result = apply_edits(text, [TextEdit("x\ny", "z", replace_all=True)])

    assert result.text == "z\r\nx\r\ny\r\n"
    assert result.replacements == 1


def test_crlf_form_is_checked_for_uniqueness_on_its_own():
    text = "keep\n-\r\nhit\r\nend\r\nhit\r\nend\r\n"

    with pytest.raises(EditError, match="occurs 2 times"):
        apply_edits(text, [TextEdit("hit\nend", "x")])
    result = apply_edits(text, [TextEdit("hit\nend", "x", replace_all=True)])

    assert result.text == "keep\n-\r\nx\r\nx\r\n"
    assert result.replacements == 2


def test_an_explicit_cr_in_old_string_is_never_converted():
    """Whoever quotes a CR means it: a lone LF is not rewritten into CRLF."""
    with pytest.raises(EditError, match="not found"):
        apply_edits("a\nb\r\nc\n", [TextEdit("a\r\nb", "x")])


def test_new_string_with_its_own_cr_is_kept_when_old_is_converted():
    result = apply_edits("a\r\nb\r\n", [TextEdit("a\nb", "x\r\ny")])

    assert result.text == "x\r\ny\r\n"


def test_mixed_file_not_found_still_hints_at_crlf():
    with pytest.raises(EditError, match="CRLF"):
        apply_edits("a\nb\r\nc\n", [TextEdit("missing\ntext", "x")])


def test_lf_only_file_gets_no_crlf_hint():
    with pytest.raises(EditError) as excinfo:
        apply_edits("a\nb\n", [TextEdit("missing", "x")])

    assert "CRLF" not in str(excinfo.value)


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


def test_window_cuts_an_oversized_first_line():
    """One huge line (minified JSON) must not come back whole."""
    window = line_window(_lines(b"x" * 100 + b"\nsecond\n"), 1, None, max_bytes=10)

    assert window.content == "x" * 10
    assert window.truncated is True
    assert window.last_line_cut is True
    assert (window.first, window.last, window.total) == (1, 1, 2)


def test_window_cuts_the_first_line_of_a_later_range():
    window = line_window(_lines(b"a\nb\n" + b"y" * 50 + b"\nz\n"), 3, None, max_bytes=8)

    assert window.content == "y" * 8
    assert (window.first, window.last, window.total) == (3, 3, 4)
    assert window.last_line_cut is True


def test_window_does_not_cut_a_first_line_that_fits_exactly():
    window = line_window(_lines(b"1234567890"), 1, None, max_bytes=10)

    assert window.content == "1234567890"
    assert window.truncated is False
    assert window.last_line_cut is False


def test_window_cut_line_is_not_flagged_when_a_later_line_overflows():
    """The first line is whole here. Only the second one does not fit."""
    window = line_window(_lines(b"short\n" + b"y" * 50 + b"\n"), 1, None, max_bytes=20)

    assert window.content == "short\n"
    assert window.truncated is True
    assert window.last_line_cut is False
    assert (window.last, window.total) == (1, 2)


def test_window_never_cuts_inside_a_multibyte_character():
    """Two-byte "é" straddles the byte limit: the half character is dropped."""
    raw = ("é" * 10).encode()  # c3 a9 repeated: byte 5 is the middle of a character

    window = line_window(_lines(raw), 1, None, max_bytes=5)

    assert window.content == "éé"
    assert window.last_line_cut is True
    assert len(window.content.encode()) <= 5


@pytest.mark.parametrize("limit", range(0, 16))
def test_window_cut_is_the_longest_valid_prefix_for_any_limit(limit):
    """Sweeps the cut over 1-, 2-, 3- and 4-byte characters."""
    text = "aé€😀b€é"
    raw = text.encode()

    window = line_window(_lines(raw), 1, None, max_bytes=limit)

    expected = max(
        (text[:i] for i in range(len(text) + 1) if len(text[:i].encode()) <= limit),
        key=len,
    )
    assert window.content == expected
    assert len(window.content.encode()) <= limit
    assert window.truncated is (len(raw) > limit)


def test_cut_first_line_with_numbers():
    window = line_window(
        _lines(b"y" * 50 + b"\nz\n"), 1, None, numbered=True, max_bytes=4
    )

    assert window.content == "     1\tyyyy"
    assert window.last_line_cut is True


def test_cut_still_rejects_invalid_utf8():
    """Cutting must not paper over bytes that were never valid UTF-8."""
    with pytest.raises(UnicodeDecodeError):
        line_window(_lines(b"ok\xff\xfe" + b"y" * 50), 1, None, max_bytes=10)


# ── is_editable_text ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("text/plain", "text/plain"),
        ("Text/Markdown; charset=UTF-8", "text/markdown"),
        ("  application/JSON ;charset=utf-8", "application/json"),
        ("", ""),
        (None, ""),
    ],
)
def test_content_type_is_normalised(raw, expected):
    assert normalize_content_type(raw) == expected


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "text/markdown",
        "text/x-python",
        "text/csv",
        "text/html",
        "Text/Plain; charset=UTF-8",
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/x-sh",
        "application/x-shellscript",
        "application/x-php",
        "application/sql",
        "application/x-tex",
        "application/x-latex",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/vnd.api+json",
        "application/atom+xml",
        "application/vnd.excalidraw+json",
    ],
)
def test_text_content_types_are_editable(content_type):
    assert is_editable_text(content_type, "/some/file") is True


@pytest.mark.parametrize(
    "content_type",
    [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "image/png",
        "image/jpeg",
        "image/gif",
        "audio/mpeg",
        "video/mp4",
        "application/zip",
        "application/gzip",
        "application/x-tar",
        "application/epub+zip",
        "application/x-msdos-program",
        "font/woff2",
    ],
)
def test_non_text_content_types_are_refused(content_type):
    assert is_editable_text(content_type, "/some/file") is False


@pytest.mark.parametrize("name", ["notes.md", "data.json", "readme.txt", "Makefile"])
def test_a_text_file_name_does_not_rescue_a_binary_content_type(name):
    """The file name only decides when the server could not tell."""
    assert is_editable_text("application/pdf", f"/docs/{name}") is False


@pytest.mark.parametrize("content_type", ["application/octet-stream", "", None])
@pytest.mark.parametrize(
    "path",
    [
        "/Makefile",
        "Dockerfile",
        "/work/LICENSE",
        "/home/.env",
        ".gitignore",
        "/a/b/.bashrc",
        "script.sh",
        "/src/app.ts",
        "/src/App.TSX",
        "/etc/app.ini",
        "/etc/app.cfg",
        "/logs/run.log",
        "/data/rows.jsonl",
        "README.",
    ],
)
def test_unclassified_files_are_editable_without_or_with_a_text_extension(
    content_type, path
):
    assert is_editable_text(content_type, path) is True


@pytest.mark.parametrize("content_type", ["application/octet-stream", "", None])
@pytest.mark.parametrize(
    "path",
    [
        "/docs/report.pdf",
        "/docs/letter.docx",
        "/docs/letter.odt",
        "/docs/sheet.xlsx",
        "/docs/slides.pptx",
        "/img/photo.png",
        "/img/photo.JPG",
        "/music/song.mp3",
        "/video/clip.mp4",
        "/archive.zip",
        "/archive.tar.gz",
        "/blob.bin",
        "/build/v1.2.3-release",
    ],
)
def test_unclassified_files_with_other_extensions_are_refused(content_type, path):
    assert is_editable_text(content_type, path) is False


@pytest.mark.parametrize(
    ("content_type", "name"),
    [
        # What Nextcloud sends on a GET: the "secure" type of its mapping
        # (resources/config/mimetypemapping.dist.json, second entry).
        ("text/markdown", "page.md"),
        ("text/plain", "config.yaml"),
        ("text/plain", "config.yml"),
        ("text/plain", "data.json"),
        ("text/plain", "feed.xml"),
        ("text/plain", "app.js"),
        ("text/plain", "logo.svg"),
        ("text/x-shellscript", "run.sh"),
        ("text/x-python", "tool.py"),
        ("text/x-php", "index.php"),
        ("text/code", "pyproject.toml"),
        ("text/plain", "app.conf"),
        ("text/csv", "table.csv"),
        ("text/calendar", "event.ics"),
        ("text/vcard", "person.vcf"),
        ("application/x-tex", "paper.tex"),
        # The primary type of the same mapping, as PROPFIND reports it.
        ("application/yaml", "config.yaml"),
        ("application/json", "data.json"),
        ("application/xml", "feed.xml"),
        ("application/javascript", "app.js"),
        ("application/x-php", "index.php"),
        ("application/toml", "pyproject.toml"),
        ("image/svg+xml", "logo.svg"),
        # No mapping at all: the server answers octet-stream.
        ("application/octet-stream", "app.ts"),
        ("application/octet-stream", "settings.ini"),
        ("application/octet-stream", "settings.cfg"),
        ("application/octet-stream", ".env"),
    ],
)
def test_what_nextcloud_assigns_to_common_text_files_is_editable(content_type, name):
    assert is_editable_text(content_type, f"/files/{name}") is True
