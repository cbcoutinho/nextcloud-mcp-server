"""A small Outlook ``.msg`` reader built directly on olefile.

Why not ``extract-msg``, which does this properly: it depends on
``red-black-tree-mod``, which PyPI publishes as an **sdist only**. The image
build runs ``uv sync --no-build`` so that no dependency's ``setup.py`` executes
at build time (docker:S8541), and that guarantee is worth more than the library.
Reading the handful of fields we index is about eighty lines.

A ``.msg`` is an OLE2 compound file. Each property lives in a stream named
``__substg1.0_<TAG><TYPE>``, where TYPE is ``001F`` for UTF-16LE text, ``001E``
for text in the message's code page, and ``0102`` for binary. Fixed-size
properties (dates, the code page itself) live packed in
``__properties_version1.0`` instead.

Reading **both** string variants is the point. markitdown's converter yields 27
bytes -- the literal ``# Email Message\\n\\n## Content`` -- on a real 1.3 MB
thread, because it looks for a body variant that message does not carry. The
same message holds a 14,996-byte plain-text body in the ``001E`` variant.
"""

import logging
import struct
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO

import olefile

logger = logging.getLogger(__name__)

# Property tags (the high 16 bits of a MAPI property tag), as hex text because
# that is how they appear in the stream names.
TAG_SUBJECT = "0037"
TAG_SENDER_NAME = "0C1A"
TAG_SENDER_SMTP = "5D01"
TAG_TO = "0E04"
TAG_CC = "0E03"
TAG_BODY = "1000"
TAG_ATTACH_LONG_NAME = "3707"
TAG_ATTACH_SHORT_NAME = "3704"

# Fixed-size properties, read from __properties_version1.0 as (tag, type).
PROP_CLIENT_SUBMIT_TIME = 0x0039
PROP_MESSAGE_DELIVERY_TIME = 0x0E06
PROP_INTERNET_CPID = 0x3FDE
PT_SYSTIME = 0x0040
PT_LONG = 0x0003

_PROPERTIES_STREAM = "__properties_version1.0"
# FILETIME counts 100-nanosecond intervals from 1601-01-01 UTC.
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
# Fallback when the message declares no code page. cp1252 rather than utf-8:
# these streams predate utf-8's ubiquity and a mis-decode of Western punctuation
# is far more likely than of multibyte text.
_DEFAULT_ENCODING = "cp1252"


class MsgReadError(Exception):
    """Raised when the container is not a readable Outlook message."""


def _stream_bytes(ole: olefile.OleFileIO, path: list[str]) -> bytes | None:
    """One stream's contents, or None when it is absent."""
    if not ole.exists("/".join(path)):
        return None
    with ole.openstream(path) as stream:
        return stream.read()


def _decode(raw: bytes, unicode_variant: bool, encoding: str) -> str:
    if unicode_variant:
        return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    return raw.decode(encoding, errors="replace").rstrip("\x00")


def _string_property(
    ole: olefile.OleFileIO, tag: str, encoding: str, prefix: list[str] | None = None
) -> str | None:
    """A string property, trying the UTF-16 variant then the code-page one.

    Both are tried because a message carries whichever its sender's client
    wrote, and assuming one is exactly the bug that makes other readers return
    an empty body.
    """
    base = list(prefix or [])
    for suffix, is_unicode in (("001F", True), ("001E", False)):
        raw = _stream_bytes(ole, base + [f"__substg1.0_{tag}{suffix}"])
        if raw:
            text = _decode(raw, is_unicode, encoding)
            if text:
                return text
    return None


def _fixed_properties(ole: olefile.OleFileIO) -> dict[int, bytes]:
    """Map ``(tag << 16 | type)`` to each fixed property's raw 8-byte value.

    The stream is a header followed by 16-byte entries: 4 bytes of property tag,
    4 of flags, 8 of value. The header is 32 bytes for a top-level message.
    """
    raw = _stream_bytes(ole, [_PROPERTIES_STREAM])
    if not raw or len(raw) < 32:
        return {}
    entries: dict[int, bytes] = {}
    for offset in range(32, len(raw) - 15, 16):
        (prop_tag,) = struct.unpack_from("<I", raw, offset)
        entries[prop_tag] = raw[offset + 8 : offset + 16]
    return entries


def _filetime(value: bytes) -> datetime | None:
    (ticks,) = struct.unpack("<Q", value)
    if not ticks:
        return None
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=ticks // 10)
    except OverflowError:
        # A corrupt or absurd timestamp must not fail the whole message.
        logger.debug("ignoring out-of-range .msg FILETIME %d", ticks)
        return None


def _attachment_names(ole: olefile.OleFileIO, encoding: str) -> list[str]:
    """Filenames of the attached items, in storage order."""
    storages = sorted(
        {
            entry[0]
            for entry in ole.listdir()
            if entry and entry[0].startswith("__attach_version1.0")
        }
    )
    names = []
    for storage in storages:
        name = _string_property(
            ole, TAG_ATTACH_LONG_NAME, encoding, prefix=[storage]
        ) or _string_property(ole, TAG_ATTACH_SHORT_NAME, encoding, prefix=[storage])
        names.append(name or "unnamed")
    return names


def read_msg(stream: BinaryIO) -> dict[str, Any]:
    """Read an Outlook message into ``{subject, sender, to, cc, date, body,
    attachments}``.

    Every field is optional: ``.msg`` is also how Outlook saves contacts, tasks
    and calendar items, and those carry no sender or body. A missing field comes
    back as ``None`` rather than raising, so such an item is still indexed by
    whatever it does have.
    """
    if not olefile.isOleFile(stream):
        raise MsgReadError("not an OLE2 compound file")
    stream.seek(0)

    ole = olefile.OleFileIO(stream)
    try:
        props = _fixed_properties(ole)

        encoding = _DEFAULT_ENCODING
        cpid_value = props.get(PROP_INTERNET_CPID << 16 | PT_LONG)
        if cpid_value:
            (codepage,) = struct.unpack_from("<I", cpid_value)
            if codepage:
                try:
                    "".encode(f"cp{codepage}")
                    encoding = f"cp{codepage}"
                except LookupError:
                    logger.debug("unknown .msg code page %s; using default", codepage)

        sent = props.get(PROP_CLIENT_SUBMIT_TIME << 16 | PT_SYSTIME) or props.get(
            PROP_MESSAGE_DELIVERY_TIME << 16 | PT_SYSTIME
        )

        return {
            "subject": _string_property(ole, TAG_SUBJECT, encoding),
            "sender": (
                _string_property(ole, TAG_SENDER_SMTP, encoding)
                or _string_property(ole, TAG_SENDER_NAME, encoding)
            ),
            "to": _string_property(ole, TAG_TO, encoding),
            "cc": _string_property(ole, TAG_CC, encoding),
            "date": _filetime(sent) if sent else None,
            "body": _string_property(ole, TAG_BODY, encoding),
            "attachments": _attachment_names(ole, encoding),
        }
    finally:
        ole.close()
