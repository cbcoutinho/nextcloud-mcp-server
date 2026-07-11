"""Shared validators for primitive types crossing system boundaries."""

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import unquote

# Nextcloud object IDs are unsigned ints from MySQL AUTO_INCREMENT, which
# starts at 1. Restrict to ASCII positive integers to exclude Unicode digit
# classes (e.g. superscripts, Arabic-Indic numerals) that pass str.isdigit()
# / str.isdecimal() but would never be valid Nextcloud IDs, and to reject "0"
# and leading zeros.
_NEXTCLOUD_DOC_ID_RE = re.compile(r"^[1-9][0-9]*$")

# A bare non-negative integer string is accepted as Unix seconds (the
# pre-RFC-3339 wire format) so older callers keep working.
_UNIX_SECONDS_RE = re.compile(r"^[0-9]+$")


def is_valid_nextcloud_doc_id(value: str) -> bool:
    """True iff `value` is the str form of a positive ASCII integer (>= 1)."""
    return bool(_NEXTCLOUD_DOC_ID_RE.fullmatch(value))


def is_safe_webdav_file_path(file_path: str) -> bool:
    """Reject traversal in a WebDAV path after repeated URL decoding."""
    decoded_path = file_path
    for _ in range(3):
        next_path = unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path

    if "\x00" in decoded_path:
        return False

    path_parts = PurePosixPath(decoded_path.replace("\\", "/").lstrip("/")).parts
    return ".." not in path_parts


def parse_modified_timestamp(
    value: str | int | float | None,
    *,
    param_name: str = "modified_at",
) -> int | None:
    """Normalize a search date-filter bound to an int Unix-second timestamp.

    ADR-027: callers (the MCP tool, the ``/api/v1`` search endpoints, and the
    visualization route) accept **RFC 3339 / ISO 8601** datetimes at the
    boundary — the ergonomic, Nextcloud-Unified-Search-style format — while the
    ``modified_at`` Qdrant payload stays an int Unix-second timestamp (a
    cross-app normalization done by the scanner). This converts the former to
    the latter so the numeric ``Range`` filter and INTEGER payload index work
    without re-indexing.

    Accepts:

    - ``None`` / empty string ⇒ ``None`` (open-ended bound).
    - ``int`` / ``float`` ⇒ truncated to int seconds.
    - A bare non-negative integer string ⇒ Unix seconds (legacy wire format).
    - An RFC 3339 / ISO 8601 string, e.g. ``"2026-01-01T00:00:00Z"`` or
      ``"2026-01-01T00:00:00+02:00"``. A naive datetime (no offset) is assumed
      to be UTC, matching the payload representation.

    Args:
        value: The raw bound from the request.
        param_name: Field name used in error messages.

    Returns:
        Int Unix-second timestamp (UTC), or ``None`` for an open bound.

    Raises:
        ValueError: If the value is negative or cannot be parsed.
    """
    if value is None:
        return None
    # bool is an int subclass — reject it explicitly so True/False aren't
    # silently read as 1/0 seconds.
    if isinstance(value, bool):
        raise ValueError(f"{param_name} must be a datetime string or Unix seconds")
    if isinstance(value, (int, float)):
        seconds = int(value)
        if seconds < 0:
            raise ValueError(f"{param_name} must be >= 0, got {seconds}")
        return seconds
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _UNIX_SECONDS_RE.fullmatch(text):
            return int(text)
        # RFC 3339 / ISO 8601. ``fromisoformat`` accepts a trailing "Z" only on
        # Python 3.11+; normalize it for safety across versions.
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"{param_name} must be an RFC 3339 datetime or Unix seconds, "
                f"got {value!r}"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    raise ValueError(f"{param_name} must be a datetime string or Unix seconds")
