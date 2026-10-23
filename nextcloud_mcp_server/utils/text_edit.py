"""Exact-match text edits and line windows for the WebDAV text tools.

Pure functions, no I/O. ``nc_webdav_edit_file`` downloads a file, runs
:func:`apply_edits` on it and writes the result back, so a caller sends only
the passage it changes instead of the whole file. ``nc_webdav_read_file`` uses
:func:`line_window` to return part of a text file.

The edit semantics follow the ``str_replace`` convention models are trained on:
``old_string`` must match exactly and occur once, unless ``replace_all`` says
otherwise. Anchoring on content rather than on line numbers keeps an edit
correct even after earlier edits have shifted the lines.
"""

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: How many occurrence line numbers an "ambiguous match" error lists.
_MAX_REPORTED_LINES = 10


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


def _to_crlf(value: str) -> str:
    """Turn bare LF into CRLF, leaving existing CRLF alone."""
    return value.replace("\r\n", "\n").replace("\n", "\r\n")


def apply_edits(text: str, edits: Sequence[TextEdit]) -> EditResult:
    """Apply ``edits`` to ``text`` in order, each to the previous result.

    All or nothing: the first edit that cannot be applied raises
    :class:`EditError` and the caller writes nothing.

    Line endings: models send ``\\n``, so in a file that uses CRLF, the bare
    LFs of an edit that contains no ``\\r`` are turned into CRLF before
    matching. The file's line endings stay as they were.

    Raises:
        EditError: No edits, an empty ``old_string``, an edit that changes
            nothing, an ``old_string`` that is not found, or one that occurs
            more than once without ``replace_all``.
    """
    if not edits:
        raise EditError("No edits given.")

    crlf = "\r\n" in text
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
        if crlf:
            if "\r" not in old:
                old = _to_crlf(old)
            if "\r" not in new:
                new = _to_crlf(new)

        count = text.count(old)
        if count == 0:
            hint = " The file uses Windows line endings (CRLF)." if crlf else ""
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
    """Part of a text file, as returned by :func:`line_window`."""

    content: str
    first: int
    last: int
    total: int
    truncated: bool


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
    is set, ``last`` then names the last line actually returned.

    Raises:
        LineRangeError: ``start`` lies past the end of the file.
        UnicodeDecodeError: The window is not valid UTF-8.
    """
    picked: list[str] = []
    picked_bytes = 0
    truncated = False
    last = start - 1
    total = 0
    for total, raw in enumerate(lines, start=1):
        if total < start or (end is not None and total > end) or truncated:
            continue
        if max_bytes is not None and picked_bytes + len(raw) > max_bytes and picked:
            truncated = True
            continue
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
        content=content, first=start, last=last, total=total, truncated=truncated
    )
