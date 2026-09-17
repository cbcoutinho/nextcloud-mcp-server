"""Document processing plugins for extracting text from various file formats."""

from nextcloud_mcp_server.config import get_settings

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .ocr import OcrProcessor
from .presentation import PptxProcessor
from .pymupdf import PyMuPDFProcessor
from .pypdfium2_fast import Pypdfium2FastProcessor
from .registry import ProcessorRegistry, get_registry

# Register processors at module initialization. The tiered PDF pipeline selects
# by tier (not priority): Pypdfium2FastProcessor is the ``fast`` tier,
# PyMuPDFProcessor the ``structured`` rollback, and a single OcrProcessor is the
# ``ocr`` tier — its backend (gateway vs direct Mistral), model (Mistral, surya,
# …), and sync/batch mode are all chosen from settings. It is reached only when
# ``document_ocr_enabled`` is set. OCR gets the lowest priority so it's never the
# non-tiered default for PDFs.
#
# This module is imported lazily (first parse), never at app startup, so reading
# settings here does not drag the parse stack onto the startup path (#877).
_settings = get_settings()
_registry = get_registry()
_registry.register(Pypdfium2FastProcessor(), priority=20)
_registry.register(
    PyMuPDFProcessor(
        extract_images=_settings.pymupdf_extract_images,
        image_dir=_settings.pymupdf_image_dir,
    ),
    priority=10,
)
_registry.register(
    OcrProcessor(
        name="ocr",
        tier="ocr",
        model_setting="document_ocr_model",
    ),
    priority=1,
)

# PPTX is OOXML, so python-pptx reads it directly -- no external service and no
# LibreOffice binary to gate on. Priority 15 puts it above the optional
# Unstructured processor (10), which also claims this type but flattens
# slide/table structure; below Docling's images-only 20, where the two never
# actually compete since Docling does not auto-select PPTX.
_registry.register(PptxProcessor(), priority=15)

__all__ = [
    "DocumentProcessor",
    "ProcessingResult",
    "ProcessorError",
    "ProcessorRegistry",
    "get_registry",
    "PptxProcessor",
    "PyMuPDFProcessor",
    "Pypdfium2FastProcessor",
    "OcrProcessor",
]
