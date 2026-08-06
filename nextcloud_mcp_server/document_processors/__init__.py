"""Document processing plugins for extracting text from various file formats."""

import logging

from nextcloud_mcp_server.config import get_settings

from . import _libreoffice
from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .msg import MsgProcessor
from .ocr import OcrProcessor
from .office import OfficeDocumentProcessor
from .pymupdf import PyMuPDFProcessor
from .pypdfium2_fast import Pypdfium2FastProcessor
from .registry import ProcessorRegistry, get_registry
from .spreadsheet import SpreadsheetProcessor

logger = logging.getLogger(__name__)

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

# Office and email formats. Priority 15 puts them above the optional
# Unstructured processor (10), which also claims these types but flattens their
# structure, while leaving 20 for docling. Each reads its format the way that
# measured best rather than sharing one route -- see the module docstrings.
_office_timeout = _settings.document_office_timeout_seconds
_registry.register(SpreadsheetProcessor(timeout=_office_timeout), priority=15)
_registry.register(MsgProcessor(), priority=15)
if _libreoffice.LIBREOFFICE_AVAILABLE:
    _registry.register(OfficeDocumentProcessor(timeout=_office_timeout), priority=15)
else:
    # Not an error: the API image has no reason to carry LibreOffice. Leaving
    # the processor unregistered means .doc/.docx report "no processor for
    # type" once, rather than every document failing mid-parse.
    logger.info(
        "LibreOffice not found; .doc/.docx indexing is unavailable in this image"
    )

__all__ = [
    "DocumentProcessor",
    "ProcessingResult",
    "ProcessorError",
    "ProcessorRegistry",
    "get_registry",
    "MsgProcessor",
    "OfficeDocumentProcessor",
    "PyMuPDFProcessor",
    "Pypdfium2FastProcessor",
    "OcrProcessor",
    "SpreadsheetProcessor",
]
