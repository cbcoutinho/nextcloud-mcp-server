"""Exact-match text edits and line windows for the WebDAV text tools.

Pure functions, no I/O. ``nc_webdav_edit_file`` downloads a file, checks with
:func:`is_editable_text` that it is plain text, runs :func:`apply_edits` on it
and writes the result back, so a caller sends only the passage it changes
instead of the whole file. ``nc_webdav_read_file`` uses :func:`line_window` to
return part of a text file.

The edit semantics follow the ``str_replace`` convention models are trained on:
``old_string`` must match exactly and occur once, unless ``replace_all`` says
otherwise. Anchoring on content rather than on line numbers keeps an edit
correct even after earlier edits have shifted the lines.
"""

import difflib
import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: How many occurrence line numbers an "ambiguous match" error lists.
_MAX_REPORTED_LINES = 10

#: Content types that are not ``text/*`` but are plain UTF-8 text. ``+json`` and
#: ``+xml`` suffix types are accepted on top of these. ``application/xhtml+xml``
#: and ``image/svg+xml`` are listed although the suffix rule covers them, since
#: SVG is the one image type this tool edits.
_TEXT_APPLICATION_TYPES = frozenset(
    {
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
        "application/x-perl",
        "application/x-php",
        "application/x-httpd-php",
        "application/sql",
        "application/x-tex",
        "application/x-latex",
        "application/xhtml+xml",
        "image/svg+xml",
    }
)

#: File extensions (lower case, no dot) that mark a file as text when the server
#: could not tell: Nextcloud answers ``application/octet-stream`` for types it
#: has no mapping for, such as ``.ts``, ``.ini`` or ``.log``. Office,
#: PDF, image, audio, video and archive formats never belong here.
_TEXT_EXTENSIONS = frozenset(
    (
        # Documents and markup
        "md markdown txt text rst adoc org tex log html htm xml svg "
        # Data
        "json jsonl ndjson yaml yml toml csv tsv ics vcf "
        # Configuration
        "ini cfg conf env properties "
        # Source code and patches
        "js mjs cjs jsx ts tsx css scss py rb php sh bash zsh sql java c h cpp "
        "go rs diff patch"
    ).split()
)

#: What a server reports for a file it cannot classify.
_UNKNOWN_CONTENT_TYPES = frozenset({"", "application/octet-stream"})


def normalize_content_type(content_type: str | None) -> str:
    """The bare media type: parameters dropped, lower case, ``""`` for none."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def is_editable_text(content_type: str | None, path: str) -> bool:
    """Whether a file is plain text that ``nc_webdav_edit_file`` may edit.

    An allow-list, so an unknown type is refused. The decode check alone is not
    enough: a PDF that happens to be all ASCII decodes as UTF-8, and a text edit
    would then shift every offset in its cross-reference table and corrupt it.

    Allowed: any ``text/*``, ``+json`` and ``+xml`` types, and the application
    types in :data:`_TEXT_APPLICATION_TYPES`. A file the server could not
    classify (``application/octet-stream`` or no type) is allowed when its name
    has no extension (``Makefile``, ``Dockerfile``, ``.env``) or one of the
    well-known text extensions. Everything else is refused: PDF, Office and
    ODF documents, images other than SVG, audio, video and archives.

    ``content_type`` is normalised first, so ``Text/Plain; charset=UTF-8``
    counts as ``text/plain``. ``path`` only matters for an unclassified file.
    """
    media_type = normalize_content_type(content_type)
    if media_type in _UNKNOWN_CONTENT_TYPES:
        extension = posixpath.splitext(posixpath.basename(path))[1].lstrip(".")
        return not extension or extension.lower() in _TEXT_EXTENSIONS
    return (
        media_type.startswith("text/")
        or media_type.endswith(("+json", "+xml"))
        or media_type in _TEXT_APPLICATION_TYPES
    )


class EditError(ValueError):
    """An edit that cannot be applied. The message is written for the caller."""


class LineRangeError(ValueError):
    """A line range outside the file. The message is written for the caller."""


@dataclass(frozen=True)
class TextEdit:
    """Replace ``old_string`` with ``new_string`` (once, or everywhere)."""

    old_string: str
    new_string: str
    replace_all: bool = False


@dataclass(frozen=True)
class EditResult:
    """The edited text and how many occurrences were replaced in total."""

    text: str
    replacements: int


def _line_of(text: str, index: int) -> int:
    """1-based line number of the character at ``index``."""
    return text.count("\n", 0, index) + 1


def _occurrence_lines(text: str, needle: str) -> list[int]:
    """1-based line numbers where ``needle`` starts, in order."""
    lines: list[int] = []
    start = text.find(needle)
    while start != -1:
        lines.append(_line_of(text, start))
        start = text.find(needle, start + len(needle))
    return lines


#: A line feed that is not part of a CRLF pair.
_BARE_LF = re.compile(r"(?<!\r)\n")


def _to_crlf(value: str) -> str:
    """Turn bare LF into CRLF, leaving existing CRLF alone."""
    return value.replace("\r\n", "\n").replace("\n", "\r\n")


def apply_edits(text: str, edits: Sequence[TextEdit]) -> EditResult:
    """Apply ``edits`` to ``text`` in order, each to the previous result.

    All or nothing: the first edit that cannot be applied raises
    :class:`EditError` and the caller writes nothing.

    Line endings: models send ``\\n``, whatever the file uses. Each edit is
    matched as given first, so an edit to an LF-only passage works in a file
    that mixes LF and CRLF. Only when ``old_string`` is not found, contains a
    line break but no ``\\r``, and the file has a CRLF passage that matches, is
    it matched in its CRLF form instead, and ``new_string`` is converted the
    same way unless it carries a ``\\r`` of its own. The two forms are never
    combined within one edit: uniqueness and ``replace_all`` apply to the form
    that matched. In a file that uses CRLF throughout, ``new_string`` is
    always converted, so an edit that adds lines keeps the file CRLF-only. The
    file's line endings stay as they were.

    Raises:
        EditError: No edits, an empty ``old_string``, an edit that changes
            nothing, an ``old_string`` that is not found, or one that occurs
            more than once without ``replace_all``.
    """
    if not edits:
        raise EditError("No edits given.")

    replacements = 0
    for number, edit in enumerate(edits, start=1):
        label = f"Edit {number} of {len(edits)}" if len(edits) > 1 else "The edit"
        old, new = edit.old_string, edit.new_string
        if not old:
            raise EditError(
                f"{label}: old_string is empty. Quote the text to replace, "
                "including enough context to make it unique."
            )
        if old == new:
            raise EditError(
                f"{label}: old_string and new_string are identical, so it would "
                "change nothing."
            )

        count = text.count(old)
        crlf_new = False
        if count == 0 and "\r" not in old and "\n" in old:
            # Not found as given. The file may still have this passage with
            # CRLF line breaks, which is how a model's "\n" quote looks there.
            crlf_old = _to_crlf(old)
            crlf_count = text.count(crlf_old)
            if crlf_count:
                old, count = crlf_old, crlf_count
                crlf_new = True
        elif count and "\r\n" in text and not _BARE_LF.search(text):
            # Found as given in a file that uses CRLF throughout (old_string
            # has no line break then). Lines the edit adds get CRLF too, so
            # the file does not end up mixing the two.
            crlf_new = True
        if crlf_new and "\r" not in new:
            new = _to_crlf(new)
        if count == 0:
            hint = (
                " The file contains Windows line endings (CRLF)."
                if "\r\n" in text
                else ""
            )
            after = " after the previous edits in this call" if number > 1 else ""
            raise EditError(
                f"{label}: old_string was not found in the file{after}. It has to "
                f"match exactly, including whitespace, indentation and line "
                f"breaks.{hint} Re-read the passage and copy it verbatim."
            )
        if count > 1 and not edit.replace_all:
            lines = _occurrence_lines(text, old)
            shown = ", ".join(str(n) for n in lines[:_MAX_REPORTED_LINES])
            more = (
                f" and {count - _MAX_REPORTED_LINES} more"
                if count > _MAX_REPORTED_LINES
                else ""
            )
            raise EditError(
                f"{label}: old_string occurs {count} times (starting on lines "
                f"{shown}{more}). Include more surrounding text so it matches "
                "exactly once, or set replace_all to replace every occurrence."
            )

        text = text.replace(old, new) if edit.replace_all else text.replace(old, new, 1)
        replacements += count if edit.replace_all else 1

    return EditResult(text=text, replacements=replacements)


def unified_diff(
    before: str, after: str, path: str, *, max_lines: int = 200
) -> tuple[str, bool]:
    """A unified diff of an edit, for checking it without re-reading the file.

    Returns ``(diff, truncated)``. Two lines of context, cut after
    ``max_lines`` lines so a sweeping ``replace_all`` cannot flood the response.
    """
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=path,
            tofile=path,
            n=2,
            lineterm="",
        )
    )
    if len(lines) <= max_lines:
        return "\n".join(lines), False
    return "\n".join(lines[:max_lines]), True


@dataclass(frozen=True)
class LineWindow:
    """Part of a text file, as returned by :func:`line_window`.

    ``truncated`` means the window stops short of the range asked for because of
    ``max_bytes``. ``last_line_cut`` says that the last line in ``content`` is
    itself incomplete: it alone was larger than ``max_bytes``.
    """

    content: str
    first: int
    last: int
    total: int
    truncated: bool
    last_line_cut: bool = False


def _utf8_prefix(raw: bytes, limit: int) -> bytes:
    """The longest prefix of ``raw`` of at most ``limit`` bytes that ends between
    two characters, so decoding it never fails on a split multi-byte character.
    """
    if len(raw) <= limit:
        return raw
    cut = limit
    # ``raw[cut]`` is the first byte left out. While it is a continuation byte
    # (0b10xxxxxx) the cut falls inside a character, so step back to its first
    # byte. A UTF-8 character has at most three continuation bytes. Invalid data
    # with more is left to the caller's decode to reject.
    for _ in range(3):
        if cut > 0 and raw[cut] & 0xC0 == 0x80:
            cut -= 1
    return raw[:cut]


def line_window(
    lines: Iterable[bytes],
    start: int,
    end: int | None,
    *,
    numbered: bool = False,
    max_bytes: int | None = None,
) -> LineWindow:
    """Lines ``start`` to ``end`` (1-based, inclusive) of a UTF-8 text file.

    Takes the file as an iterable of byte lines (an open binary file works), so
    a large file is never held in memory, only the window. Only the window is
    decoded. Without ``numbered`` the content is returned byte-exact, line
    endings included. With it, each line is prefixed by its number and a tab,
    the way ``cat -n`` prints it.

    ``max_bytes`` caps the window: lines past it are left out and ``truncated``
    is set, ``last`` then names the last line actually returned. A first line
    that is larger than ``max_bytes`` by itself is neither dropped nor returned
    whole. Its start is returned, as much as fits in ``max_bytes`` without
    splitting a multi-byte character, with ``truncated`` and ``last_line_cut``
    set. The rest of that line is not available, but the lines after it are.

    Raises:
        LineRangeError: ``start`` lies past the end of the file.
        UnicodeDecodeError: The window is not valid UTF-8.
    """
    picked: list[str] = []
    picked_bytes = 0
    truncated = False
    cut = False
    last = start - 1
    total = 0
    for total, raw in enumerate(lines, start=1):
        if total < start or (end is not None and total > end) or truncated:
            continue
        if max_bytes is not None and picked_bytes + len(raw) > max_bytes:
            truncated = True
            if picked:
                continue
            # Nothing fits yet and this line alone is over the cap: return its
            # start rather than the whole line, which can be megabytes long.
            raw = _utf8_prefix(raw, max_bytes)
            cut = True
        line = raw.decode("utf-8")
        picked_bytes += len(raw)
        if numbered:
            bare = line.rstrip("\r\n")
            picked.append(f"{total:>6}\t{bare}")
        else:
            picked.append(line)
        last = total

    # An empty file has no line 1, but asking for it is not an error.
    if start > max(total, 1):
        raise LineRangeError(
            f"line_start={start} is past the end of the file, which has {total} lines."
        )
    content = "\n".join(picked) if numbered else "".join(picked)
    return LineWindow(
        content=content,
        first=start,
        last=last,
        total=total,
        truncated=truncated,
        last_line_cut=cut,
    )
