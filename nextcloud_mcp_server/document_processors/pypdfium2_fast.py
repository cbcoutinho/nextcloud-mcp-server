"""Tier-1 fast PDF text extractor (pypdfium2).

A permissively-licensed (Apache/BSD-2) fast path that extracts a PDF's text
layer + page boundaries WITHOUT pymupdf4llm's expensive O(n^2) table/graphics
analysis. For born-digital PDFs (the tier-0 classifier's ``fast`` verdict) this
returns clean text in well under a second -- including the form/table PDFs that
timed out under pymupdf4llm (e.g. ``Student 1a.pdf``: 120s timeout -> ~1s here).

bbox is re-derived from the PDF bytes + ``page_boundaries`` by
``search/pdf_highlighter``, so this processor only needs to emit ``text`` and
``metadata["page_boundaries"]`` for chunk highlighting to keep working.

It deliberately does NOT recover tables/layout; a low-quality result is meant to
escalate to the ``structured`` tier (pymupdf4llm, graphics_limit-guarded) via the
registry (B2 escalation wiring).
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from .base import DocumentProcessor, ProcessingResult

logger = logging.getLogger(__name__)


def _extract(content: bytes) -> tuple[str, dict[str, Any]]:
    """Extract concatenated text + metadata from a PDF (runs in a worker thread).

    ``page_boundaries`` offsets index into the returned text, which is the page
    texts joined with no separator so the offsets stay exact (the contract
    ``search/pdf_highlighter`` and the chunker rely on).
    """
    import pypdfium2 as pdfium  # noqa: PLC0415 -- keep the native import lazy

    from nextcloud_mcp_server.document_processors._native_locks import (  # noqa: PLC0415
        pdfium_serialized,
    )

    # PDFium is not thread-safe (shared process-global library + an unlocked
    # module-global object tracker), so serialize: concurrent ingest jobs must not
    # drive it from two worker threads at once.
    with pdfium_serialized():
        pdf = pdfium.PdfDocument(content)
        try:
            page_texts: list[str] = []
            for i in range(len(pdf)):
                page = pdf[i]
                try:
                    textpage = page.get_textpage()
                    try:
                        page_texts.append(textpage.get_text_bounded() or "")
                    finally:
                        textpage.close()
                finally:
                    # Outer finally so the page handle is freed even if
                    # get_textpage() raises on a corrupt page.
                    page.close()
            doc_meta = pdf.get_metadata_dict() or {}
        finally:
            pdf.close()

    page_boundaries: list[dict[str, Any]] = []
    offset = 0
    for n, text in enumerate(page_texts, start=1):
        page_boundaries.append(
            {"page": n, "start_offset": offset, "end_offset": offset + len(text)}
        )
        offset += len(text)

    full_text = "".join(page_texts)
    metadata: dict[str, Any] = {
        "page_count": len(page_texts),
        "page_boundaries": page_boundaries,
    }
    title = doc_meta.get("Title")
    if title:
        metadata["title"] = title
    return full_text, metadata


class Pypdfium2FastProcessor(DocumentProcessor):
    """Tier-1 fast PDF text extractor backed by pypdfium2."""

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
        self,
        content: bytes,
        content_type: str,
        filename: str | None = None,
        options: dict[str, Any] | None = None,
        progress_callback: (
            Callable[[float, float | None, str | None], Awaitable[None]] | None
        ) = None,
    ) -> ProcessingResult:
        if progress_callback:
            await progress_callback(0, 100, "Extracting text (pypdfium2)")
        try:
            full_text, metadata = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                _extract, content
            )
        except Exception as e:
            # Fast path is best-effort: a failure here escalates rather than
            # crashing the pipeline. pypdfium2 has no O(n^2) bomb, so this is a
            # genuinely malformed PDF, not a resource blowup.
            logger.warning(
                "pypdfium2 fast extract failed for %s: %s", filename or "<bytes>", e
            )
            return ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "error"},
                processor=self.name,
                success=False,
                error=f"{type(e).__name__}: {e}",
            )
        metadata["file_size"] = len(content)
        if progress_callback:
            await progress_callback(100, 100, "Done")
        return ProcessingResult(text=full_text, metadata=metadata, processor=self.name)

    async def health_check(self) -> bool:
        try:
            import pypdfium2  # noqa: F401, PLC0415 -- availability probe

            return True
        except Exception:
            return False
