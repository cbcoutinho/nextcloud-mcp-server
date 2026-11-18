"""Unit tests for the exact-match edit and line-window helpers (utils/text_edit)."""

import io

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

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

#: A small alphabet so a quoted passage often occurs once, often several
#: times, and line breaks fall inside and at the edges of quotes.
_lf_text = st.text(alphabet="ab\n", max_size=30)


@st.composite
def lf_edits(draw):
    """An LF-only file and an edit as a model sends it: ``\\n`` line breaks,
    ``old_string`` usually quoted from the file itself."""
    text = draw(_lf_text)
    if text and draw(st.booleans()):
        start = draw(st.integers(0, len(text) - 1))
        old = text[start : draw(st.integers(start + 1, len(text)))]
    else:
        old = draw(st.text(alphabet="ab\n", min_size=1, max_size=30))
    edit = TextEdit(old, draw(_lf_text), replace_all=draw(st.booleans()))
    return text, edit


def _outcome(text: str, edit: TextEdit) -> tuple[str, int] | None:
    try:
        result = apply_edits(text, [edit])
    except EditError:
        return None
    return result.text, result.replacements


@given(lf_edits())
@example(("one\ntwo\nthree\n", TextEdit("two", "2")))
@example(("keep\ndrop me\nkeep\n", TextEdit("drop me\n", "")))
@example(("a-b-a-b\n", TextEdit("a", "c", replace_all=True)))
def test_an_lf_file_is_edited_like_str_replace(case):
    """Exactly one match, or replace_all: plain replacement. Anything else is
    an error, never a silent no-op or a guess between matches."""
    text, edit = case
    count = text.count(edit.old_string)

    outcome = _outcome(text, edit)

    if (
        count
        and edit.old_string != edit.new_string
        and (edit.replace_all or count == 1)
    ):
        assert outcome == (text.replace(edit.old_string, edit.new_string), count)
    else:
        assert outcome is None


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


@given(lf_edits())
@example(("a\nb\nc\n", TextEdit("a\nb", "x\ny")))  # \n quote matches CRLF
@example(("a\nb\na\nb\n", TextEdit("b", "B", replace_all=True)))
@example(("a\nb\na\nb\n", TextEdit("a\nb", "x\ny", replace_all=True)))
@example(("title\nbody\n", TextEdit("title", "title\nsubtitle")))  # added lines
# A quote starting with "\n" used to match the LF half of a CRLF pair and
# leave a lone CR behind: deleting "\nb" gave "a\r\r\nc\r\n".
@example(("a\nb\nc\n", TextEdit("\nb", "")))
@example(("a\nb\nc\n", TextEdit("\n", "", replace_all=True)))
def test_a_crlf_file_is_edited_as_if_it_were_lf(case):
    """Models quote with ``\\n`` whatever the file uses. In a file that is CRLF
    throughout, an edit must do exactly what it does to the LF version of the
    file, and the file must stay CRLF-only."""
    lf_text, edit = case
    assume("\n" in lf_text)  # no line endings, so no convention to keep

    expected = _outcome(lf_text, edit)

    outcome = _outcome(_crlf(lf_text), edit)

    if expected is None:
        assert outcome is None
    else:
        assert outcome == (_crlf(expected[0]), expected[1])


def test_a_quote_ending_in_cr_does_not_match_half_a_crlf_pair():
    """Outside the property above (models do not quote CRs): "a\\r" would take
    the CR and leave a lone LF ("x\\nb"). A bare CR still matches where it is."""
    with pytest.raises(EditError, match="not found"):
        apply_edits("a\r\nb\r\n", [TextEdit("a\r", "x")])

    assert apply_edits("a\rb\r\n", [TextEdit("a\r", "x")]).text == "xb\r\n"
    # replace_all takes only the bare one, never the CR of the CRLF.
    result = apply_edits("a\rb\ra\r\n", [TextEdit("a\r", "x", replace_all=True)])
    assert (result.text, result.replacements) == ("xb\ra\r\n", 1)


def test_the_crlf_guards_do_not_over_reject_bare_line_breaks():
    """In a mixed file, a leading-LF quote after a bare LF still matches."""
    assert apply_edits("a\nb\r\nc", [TextEdit("\nb", "")]).text == "a\r\nc"


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


def test_crlf_mismatch_hint_in_not_found_error():
    with pytest.raises(EditError, match="CRLF"):
        apply_edits("a\r\nb\r\n", [TextEdit("missing", "x")])


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


def _longest_utf8_prefix(line: str, limit: int) -> str:
    return max(
        (line[:i] for i in range(len(line) + 1) if len(line[:i].encode()) <= limit),
        key=len,
    )


@st.composite
def windows(draw):
    """A UTF-8 file (any line endings, multi-byte characters) and a request
    for a window of it: (raw, start, end, max_bytes)."""
    lines = draw(st.lists(st.text(st.characters(exclude_characters="\n")), max_size=6))
    raw = "\n".join(lines).encode() + draw(st.sampled_from([b"", b"\n"]))
    total = len(io.BytesIO(raw).readlines())
    start = draw(st.integers(1, total + 2))
    end = draw(st.none() | st.integers(start, start + 4))
    max_bytes = draw(st.none() | st.integers(0, 40))
    return raw, start, end, max_bytes


@given(windows())
@example((b"one\r\ntwo\r\nthree\r\n", 2, 3, None))  # byte-exact, CRLF kept
@example((b"one\ntwo", 1, 99, None))  # end past the file is clamped
@example((b"one\ntwo\n", 3, None, None))  # start past the end
@example((b"", 1, None, None))  # an empty file has no line 1, yet reads as empty
@example((b"aaaa\nbbbb\ncccc\n", 1, None, 10))  # capped by bytes
@example((b"x" * 100 + b"\nsecond\n", 1, None, 10))  # oversized first line is cut
@example((b"a\nb\n" + b"y" * 50 + b"\nz\n", 3, None, 8))  # ...in a later range too
@example((b"1234567890", 1, None, 10))  # fits exactly: not cut
@example((b"short\n" + b"y" * 50 + b"\n", 1, None, 20))  # later overflow: not cut
@example((("é" * 10).encode(), 1, None, 5))  # never cut inside a character
@example(("aé€😀b€é".encode(), 1, None, 9))  # 1-, 2-, 3- and 4-byte characters
def test_line_window_returns_whole_lines_or_the_longest_valid_prefix(case):
    """Whole lines while they fit under ``max_bytes``. A first line too big on
    its own is returned as its longest prefix that does not split a character,
    so a huge line (minified JSON) neither comes back whole nor fails to decode.
    """
    raw, start, end, max_bytes = case
    lines = [line.decode() for line in io.BytesIO(raw).readlines()]
    total = len(lines)

    if start > max(total, 1):
        with pytest.raises(LineRangeError, match=f"has {total} lines"):
            line_window(_lines(raw), start, end, max_bytes=max_bytes)
        return

    window = line_window(_lines(raw), start, end, max_bytes=max_bytes)

    wanted = lines[start - 1 : end]
    fit = 0
    while fit < len(wanted) and (
        max_bytes is None or len("".join(wanted[: fit + 1]).encode()) <= max_bytes
    ):
        fit += 1
    if fit == 0 and wanted:
        assert window.content == _longest_utf8_prefix(wanted[0], max_bytes)
        assert window.last_line_cut is True
        assert window.last == start
    else:
        assert window.content == "".join(wanted[:fit])
        assert window.last_line_cut is False
        assert window.last == start - 1 + fit
    assert window.truncated is (fit < len(wanted))
    assert (window.first, window.total) == (start, total)
    if max_bytes is not None:
        assert len(window.content.encode()) <= max_bytes


def test_window_with_numbers():
    window = line_window(_lines(b"one\ntwo\nthree\n"), 2, None, numbered=True)

    assert window.content == "     2\ttwo\n     3\tthree"


def test_only_the_window_is_decoded():
    """A binary tail outside the window does not spoil reading the text head."""
    window = line_window(_lines(b"text\n\xff\xfe\n"), 1, 1)

    assert window.content == "text\n"


def test_window_that_is_not_utf8_raises():
    with pytest.raises(UnicodeDecodeError):
        line_window(_lines(b"text\n\xff\xfe\n"), 2, 2)


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

_token = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,15}", fullmatch=True)


@st.composite
def content_type_headers(draw):
    """A Content-Type header as servers send it, and its bare media type."""
    media_type = f"{draw(_token)}/{draw(_token)}"
    cased = "".join(c.upper() if draw(st.booleans()) else c.lower() for c in media_type)
    pad = st.sampled_from(["", " ", "  "])
    params = draw(st.sampled_from(["", ";charset=utf-8", " ; charset=UTF-8"]))
    return draw(pad) + cased + draw(pad) + params, media_type.lower()


@given(content_type_headers())
@example(("Text/Markdown; charset=UTF-8", "text/markdown"))
@example(("", ""))
@example((None, ""))
def test_content_type_is_normalised(case):
    raw, expected = case
    assert normalize_content_type(raw) == expected


#: Directories a file may sit in. Only the base name may ever matter, and
#: only for a file the server could not classify -- a dotted directory such as
#: ``v1.2`` must never be read as an extension.
directories = st.from_regex(
    r"(/[A-Za-z0-9._ -]{1,10}){0,3}/", fullmatch=True
) | st.just("")

TEXT_CONTENT_TYPES = [
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
]

NON_TEXT_CONTENT_TYPES = [
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
]


@given(directories, st.text(max_size=20))
@example("/docs/", "notes.md")  # a text name does not rescue a binary type
@example("/docs/", "Makefile")
def test_a_classified_content_type_decides_whatever_the_file_name(directory, name):
    """The file name only decides when the server could not tell."""
    path = directory + name
    assert [ct for ct in TEXT_CONTENT_TYPES if not is_editable_text(ct, path)] == []
    assert [ct for ct in NON_TEXT_CONTENT_TYPES if is_editable_text(ct, path)] == []


#: What a server reports for a file it cannot classify.
unclassified = st.sampled_from(
    [None, "", "application/octet-stream", "Application/Octet-Stream; x=y"]
)

TEXT_NAMES = [
    "Makefile",
    "Dockerfile",
    "LICENSE",
    ".env",
    ".gitignore",
    ".bashrc",
    "script.sh",
    "app.ts",
    "App.TSX",
    "app.ini",
    "app.cfg",
    "run.log",
    "rows.jsonl",
    "README.",
]

NON_TEXT_NAMES = [
    "report.pdf",
    "letter.docx",
    "letter.odt",
    "sheet.xlsx",
    "slides.pptx",
    "photo.png",
    "photo.JPG",
    "song.mp3",
    "clip.mp4",
    "archive.zip",
    "archive.tar.gz",
    "blob.bin",
    "v1.2.3-release",
]


@given(unclassified, directories)
def test_an_unclassified_file_is_judged_by_its_extension(content_type, directory):
    """No extension or a well-known text one: editable. Anything else is not."""
    refused = [
        n for n in TEXT_NAMES if not is_editable_text(content_type, directory + n)
    ]
    allowed = [
        n for n in NON_TEXT_NAMES if is_editable_text(content_type, directory + n)
    ]
    assert refused == []
    assert allowed == []


#: What Nextcloud sends for common text files: the "secure" type of its mapping
#: (resources/config/mimetypemapping.dist.json, second entry) on a GET, the
#: primary type as PROPFIND reports it, or octet-stream with no mapping at all.
NEXTCLOUD_ASSIGNED = [
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
    ("application/yaml", "config.yaml"),
    ("application/json", "data.json"),
    ("application/xml", "feed.xml"),
    ("application/javascript", "app.js"),
    ("application/x-php", "index.php"),
    ("application/toml", "pyproject.toml"),
    ("image/svg+xml", "logo.svg"),
    ("application/octet-stream", "app.ts"),
    ("application/octet-stream", "settings.ini"),
    ("application/octet-stream", "settings.cfg"),
    ("application/octet-stream", ".env"),
]


@given(directories)
def test_what_nextcloud_assigns_to_common_text_files_is_editable(directory):
    refused = [
        (ct, n)
        for ct, n in NEXTCLOUD_ASSIGNED
        if not is_editable_text(ct, directory + n)
    ]
    assert refused == []
