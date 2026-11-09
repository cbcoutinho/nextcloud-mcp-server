"""Plain-text files (``.txt``, ``.md``, ``.csv``), indexed as they are.

There is nothing to parse, only bytes to decode. UTF-8 (with or without a BOM)
is tried first; anything else falls back to cp1252, the usual encoding of text
saved on Windows, and finally latin-1, which accepts every byte, so a file is
never rejected for its encoding.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from .base import DocumentProcessor, ProcessingResult

TEXT_MIME_TYPES = {"text/plain", "text/markdown", "text/csv"}


def decode_text(content: bytes) -> tuple[str, str]:
    """``content`` as text, and the encoding that read it."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1"), "latin-1"


class TextProcessor(DocumentProcessor):
    """Decode a text file; no extraction needed."""

    @property
    def name(self) -> str:
        return "text"

    @property
    def tier(self) -> str:
        return "fast"

    @property
    def supported_mime_types(self) -> set[str]:
        return TEXT_MIME_TYPES

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
        text, encoding = decode_text(content)
        return ProcessingResult(
            text=text,
            metadata={"text_length": len(text), "encoding": encoding},
            processor=self.name,
        )

    async def health_check(self) -> bool:
        return True
