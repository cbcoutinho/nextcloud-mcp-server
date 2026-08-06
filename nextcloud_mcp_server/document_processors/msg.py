"""Outlook ``.msg`` messages, read with extract-msg.

The alternatives were measured and rejected on a real 1.3 MB thread:

* markitdown emitted **27 bytes** -- the literal string ``# Email Message\\n\\n##
  Content``. Its converter pokes at OLE streams directly and only finds an
  uncompressed plain-text body; a modern Outlook message stores the body as
  compressed RTF, so it silently yields nothing at all.
* docling-serve returned ``status=skipped``.
* LibreOffice cannot open ``.msg``.
* unstructured recovered the body but took 22.4s.

extract-msg recovered 15,303 characters plus headers and attachment names in
0.15s, so it is the only reasonable choice rather than merely the best one.
"""

import io
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from anyio.to_thread import run_sync

from ._msg_reader import read_msg
from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)

MSG_MIME_TYPES = {
    "application/vnd.ms-outlook",
    "application/x-msg",
}

# Names of the files attached to the message, in order. Kept separate from the
# body text so a later pass can fetch and index the attachments themselves --
# an attached contract is usually the part worth searching, and it is invisible
# in the body.
ATTACHMENT_NAMES_KEY = "attachment_names"


class MsgProcessor(DocumentProcessor):
    """Extract an Outlook message as headers + body markdown."""

    @property
    def name(self) -> str:
        return "msg"

    @property
    def tier(self) -> str:
        return "fast"

    @property
    def supported_mime_types(self) -> set[str]:
        return MSG_MIME_TYPES

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
        progress_callback: Optional[
            Callable[[float, Optional[float], Optional[str]], Awaitable[None]]
        ] = None,
    ) -> ProcessingResult:
        try:
            text, metadata = await run_sync(_extract_msg, content)
        except ProcessorError:
            raise
        except Exception as exc:
            raise ProcessorError(f"Outlook message parse failed: {exc}") from exc

        metadata["text_length"] = len(text)
        metadata["parse_mode"] = "markdown"
        return ProcessingResult(
            text=text,
            metadata=metadata,
            processor=self.name,
            success=True,
        )

    async def health_check(self) -> bool:
        try:
            import olefile  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True


def _header_line(label: str, value: Any) -> str | None:
    """``**Label:** value`` when there is a value, else nothing."""
    if value in (None, ""):
        return None
    return f"**{label}:** {value}"


def _extract_msg(content: bytes) -> tuple[str, dict[str, Any]]:
    """Message body plus its envelope. Runs in a worker thread."""
    message = read_msg(io.BytesIO(content))

    attachments = message["attachments"]
    date = message["date"]
    body = message["body"] or ""

    # The envelope is indexed as part of the text, not just held as metadata:
    # "who sent this and when" is a large share of what anyone searches an
    # inbox for, and metadata is not embedded.
    header_lines = [
        line
        for line in (
            _header_line("From", message["sender"]),
            _header_line("To", message["to"]),
            _header_line("Cc", message["cc"]),
            _header_line("Date", date.isoformat() if date else None),
            _header_line("Attachments", ", ".join(attachments)),
        )
        if line
    ]
    heading = f"# {message['subject'] or '(no subject)'}\n\n"
    text = heading + "\n".join(header_lines) + f"\n\n{body.strip()}\n"

    metadata: dict[str, Any] = {
        "subject": message["subject"],
        "sender": message["sender"],
        "date": date.isoformat() if date else None,
        ATTACHMENT_NAMES_KEY: attachments,
        "attachment_count": len(attachments),
    }
    return text, metadata
