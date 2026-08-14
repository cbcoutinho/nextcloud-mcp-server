"""Dispatch of a mime type no registered processor claims (Deck #1016).

The registry used to raise ``ProcessorError`` from dispatch. That raise unwinds
past the ingest caller's ``if not result.success:`` dead-letter block and lands
in the generic (transient-fault) retry handler, so the scanner re-queued the
document every cycle forever — the 12 ``text/html`` documents on the dev tenant.
"Nothing is registered for this type" is permanent, so dispatch must fail
gracefully instead.
"""

from __future__ import annotations

import pytest

from nextcloud_mcp_server.document_processors.base import (
    DocumentProcessor,
    ProcessingResult,
)
from nextcloud_mcp_server.document_processors.registry import ProcessorRegistry
from nextcloud_mcp_server.document_processors.source import MemoryDocumentSource

pytestmark = pytest.mark.unit

HTML = "text/html; charset=UTF-8"


class _PdfOnly(DocumentProcessor):
    """Stands in for the PDF-only set a production deployment registers."""

    @property
    def name(self) -> str:
        return "pypdfium2_fast"

    @property
    def tier(self) -> str:
        return "fast"

    @property
    def supported_mime_types(self) -> set[str]:
        return {"application/pdf"}

    async def process(
        self, content, content_type, filename=None, options=None, progress_callback=None
    ) -> ProcessingResult:  # pragma: no cover - never reached for text/html
        return ProcessingResult(
            text="x", metadata={}, processor=self.name, success=True
        )

    async def health_check(self) -> bool:  # pragma: no cover - not exercised
        return True


def _registry() -> ProcessorRegistry:
    registry = ProcessorRegistry()
    registry.register(_PdfOnly(), priority=20)
    return registry


async def test_process_source_unsupported_type_fails_without_raising() -> None:
    result = await _registry().process_source(
        MemoryDocumentSource(content=b"<html>", content_type=HTML, filename="m.html")
    )

    assert result.success is False
    assert result.metadata["parse_failed_reason"] == "unsupported_type"
    assert "No processor found" in (result.error or "")


async def test_process_bytes_unsupported_type_fails_without_raising() -> None:
    result = await _registry().process(b"<html>", HTML, filename="m.html")

    assert result.success is False
    assert result.metadata["parse_failed_reason"] == "unsupported_type"


async def test_unknown_forced_processor_still_raises() -> None:
    """Only *dispatch by mime type* degrades: a caller naming a processor that
    does not exist is a programming/config error, not a document property."""
    from nextcloud_mcp_server.document_processors.base import ProcessorError

    with pytest.raises(ProcessorError, match="nope"):
        await _registry().process(b"x", "application/pdf", processor_name="nope")
