"""Word-processor documents, read through a LibreOffice PDF rendition."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from . import _libreoffice
from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .pymupdf import PyMuPDFProcessor

logger = logging.getLogger(__name__)

DOC_MIME_TYPES = {
    "application/msword",  # legacy .doc (OLE2)
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Metadata key naming the source format a rendition was produced from. Its
# presence is what tells downstream consumers that the geometry in this result
# (page_boundaries, and later the chunk bboxes derived from them) belongs to a
# derived PDF rather than to the file the user has in Nextcloud -- so a viewer
# must fetch the rendition, not the original path.
RENDERED_FROM_KEY = "rendered_from_mime"


class OfficeDocumentProcessor(DocumentProcessor):
    """Extract ``.doc``/``.docx`` by rendering to PDF, then parsing that.

    Why a rendition rather than reading the document directly:

    * Legacy ``.doc`` has no working pure-Python reader at all.
    * For ``.docx`` the rendition is *more* faithful, not less. Measured against
      a direct ``mammoth`` parse on a 4-column questionnaire, the rendition
      recalls 98.3% of the same tokens and reproduces 22 table rows against 17
      -- because ``mammoth``'s HTML drops a vertically-merged cell and shifts
      every remaining cell in that row one column left, so answers land under
      the wrong heading. Rendering resolves merged cells the way a reader sees
      them.
    * The rendition is a real PDF, so page numbers and highlight geometry come
      from the existing PDF path instead of needing a second implementation.

    The trade-off is that hyperlink targets are lost -- LibreOffice renders the
    display text and drops the href.
    """

    def __init__(self, timeout: float = 120.0):
        self._timeout = timeout
        # Forced structured tier, deliberately not the classifier's choice. The
        # classifier scores a rendition on text quality and returns
        # ``tier='fast'`` for these documents, and the fast tier extracts ZERO
        # tables from a rendition: rendered table borders are vector line-art,
        # so only the structured tier's find_tables recovers the grid. Letting
        # the ladder choose would discard exactly the structure the rendition
        # exists to preserve.
        self._pdf = PyMuPDFProcessor(extract_images=False)

    @property
    def name(self) -> str:
        return "office"

    @property
    def tier(self) -> str:
        return "structured"

    @property
    def supported_mime_types(self) -> set[str]:
        return DOC_MIME_TYPES

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
        """Render to PDF, then extract with the structured PDF processor."""
        name = filename or _default_name(content_type)
        if progress_callback:
            await progress_callback(0.0, None, "Rendering document to PDF...")

        try:
            pdf_bytes = await _libreoffice.convert(
                content, name, "pdf", timeout=self._timeout
            )
        except _libreoffice.LibreOfficeError as exc:
            raise ProcessorError(f"Office rendition failed: {exc}") from exc

        result = await self._pdf.process(
            pdf_bytes, "application/pdf", name, options, progress_callback
        )
        result.metadata[RENDERED_FROM_KEY] = content_type
        result.metadata["rendition_bytes"] = len(pdf_bytes)
        result.processor = self.name
        return result

    async def health_check(self) -> bool:
        return _libreoffice.LIBREOFFICE_AVAILABLE


def _default_name(content_type: str) -> str:
    """A filename whose extension selects the right LibreOffice import filter.

    Only used when the caller passed none: LibreOffice picks its filter from the
    extension, so ``document`` with no suffix imports as the wrong format.
    """
    if content_type.split(";")[0].strip().lower() == "application/msword":
        return "document.doc"
    return "document.docx"
