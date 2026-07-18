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

from nextcloud_mcp_server.config import get_settings

from .base import DocumentProcessor, ProcessingResult
from .source import DocumentSource, resolve_path

logger = logging.getLogger(__name__)


def _extract_window(
    pdfium: Any, content: bytes | str, start: int, end: int, page_texts: list[str]
) -> None:
    """Extract pages ``[start, end)`` into ``page_texts`` from a fresh document.

    Opening and closing the document per window is what bounds memory -- see
    ``_extract``. Callers hold the pdfium lock.
    """
    pdf = pdfium.PdfDocument(content)
    try:
        for i in range(start, end):
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
    finally:
        pdf.close()


def _extract(
    content: bytes | str, page_window: int = 100
) -> tuple[str, dict[str, Any]]:
    """Extract concatenated text + metadata from a PDF (runs in a worker thread).

    ``content`` is either the PDF bytes or a path to it. A path is preferable:
    ``PdfDocument(path)`` uses ``FPDF_LoadDocument``, which reads incrementally,
    whereas the bytes form uses ``FPDF_LoadMemDocument64`` and pins the whole
    buffer for the document's lifetime. Note this bounds the *input* copy only --
    the parse working set is bounded by ``page_window`` below.

    ``page_boundaries`` offsets index into the returned text, which is the page
    texts joined with no separator so the offsets stay exact (the contract
    ``search/pdf_highlighter`` and the chunker rely on).

    Pages are extracted in windows of ``page_window``, re-opening the document
    for each window. PDFium retains parsed page objects for the document's
    lifetime -- ``page.close()`` does not return them -- so a single open scales
    peak RSS with page count (a real 4003-page file measured 1914 MB). Windowing
    caps that at one window's worth; the freed arena is reused by the next
    window, so peak stays flat (100 pages -> 63 MB, unchanged output). Pass 0 to
    disable and extract in a single open.
    """
    import pypdfium2 as pdfium  # noqa: PLC0415 -- keep the native import lazy

    from nextcloud_mcp_server.document_processors._native_locks import (  # noqa: PLC0415
        pdfium_serialized,
    )

    # PDFium is not thread-safe (shared process-global library + an unlocked
    # module-global object tracker), so serialize: concurrent ingest jobs must not
    # drive it from two worker threads at once. The lock is taken per window
    # rather than for the whole extraction: no pdfium object is held across a
    # window boundary, so releasing there is safe and stops one large document
    # blocking every other ingest job for the duration of its parse.
    with pdfium_serialized():
        pdf = pdfium.PdfDocument(content)
        try:
            page_count = len(pdf)
            doc_meta = pdf.get_metadata_dict() or {}
        finally:
            pdf.close()

    page_texts: list[str] = []
    window = page_window if page_window > 0 else page_count
    start = 0
    while start < page_count:
        end = min(start + window, page_count)
        with pdfium_serialized():
            _extract_window(pdfium, content, start, end, page_texts)
        start = end

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

    async def process_source(
        self,
        source: DocumentSource,
        options: dict[str, Any] | None = None,
        progress_callback: (
            Callable[[float, float | None, str | None], Awaitable[None]] | None
        ) = None,
    ) -> ProcessingResult:
        """Extract from the source's path, so the bytes are never materialised."""
        # resolve_path, not source.path(): an in-memory source materialises by
        # writing to disk, which must not block the shared event loop.
        source_path = await resolve_path(source)
        return await self._extract_to_result(
            str(source_path), source.size, source.filename, progress_callback
        )

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
        return await self._extract_to_result(
            content, len(content), filename, progress_callback
        )

    async def _extract_to_result(
        self,
        content: bytes | str,
        size: int,
        filename: str | None,
        progress_callback: (
            Callable[[float, float | None, str | None], Awaitable[None]] | None
        ) = None,
    ) -> ProcessingResult:
        if progress_callback:
            await progress_callback(0, 100, "Extracting text (pypdfium2)")
        settings = get_settings()
        try:
            full_text, metadata = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                _extract, content, settings.document_parse_page_window
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
        metadata["file_size"] = size
        if progress_callback:
            await progress_callback(100, 100, "Done")
        return ProcessingResult(text=full_text, metadata=metadata, processor=self.name)

    async def health_check(self) -> bool:
        try:
            import pypdfium2  # noqa: F401, PLC0415 -- availability probe

            return True
        except Exception:
            return False
