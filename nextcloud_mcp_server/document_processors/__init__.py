"""Document processing plugins for extracting text from various file formats."""

from nextcloud_mcp_server.config import get_settings

from ._ooxml import PictureCaptioner
from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .collabora import CollaboraProcessor
from .msg import MsgProcessor
from .ocr import OcrProcessor
from .presentation import PptxProcessor
from .pymupdf import PyMuPDFProcessor
from .pypdfium2_fast import Pypdfium2FastProcessor
from .registry import ProcessorRegistry, get_registry
from .spreadsheet import XlsxProcessor
from .word import DocxProcessor

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
#
# Picture captioning (ADR-037, OFFICE_CAPTION_IMAGES) reuses the same
# docling-serve instance as the images-only DoclingProcessor and the docling
# OCR backend, read straight off Settings rather than the app.py-only
# processors-dict path those two are wired from -- docling_api_url already
# lives on the Settings dataclass for exactly this kind of second touchpoint.
_docling_ocr_lang = [
    s.strip() for s in (_settings.docling_ocr_lang or "").split(",") if s.strip()
] or None
_captioner = PictureCaptioner(
    caption_images=_settings.office_caption_images,
    docling_api_url=_settings.docling_api_url,
    caption_max_images=_settings.office_caption_max_images,
    caption_timeout=_settings.office_caption_timeout_seconds,
    docling_pipeline=_settings.docling_pipeline,
    docling_vlm_preset=_settings.docling_vlm_preset,
    docling_ocr_lang=_docling_ocr_lang,
)
_readers = {
    "pptx": PptxProcessor(captioner=_captioner),
    # Same reasoning, same priority, for .docx and .xlsx (ADR-038).
    "docx": DocxProcessor(captioner=_captioner),
    "xlsx": XlsxProcessor(captioner=_captioner),
}
for _reader in _readers.values():
    _registry.register(_reader, priority=15)

# Outlook .msg is OLE2, read in-process with olefile -- no service needed.
_registry.register(MsgProcessor(), priority=15)

# Legacy .doc/.xls/.ppt and ODF .odt/.ods/.odp go to a shared Collabora Online
# service for conversion to OOXML, then to the readers above (ADR-039). Only
# registered when a URL is configured, so an absent service means "no processor
# for this type" once, not a failed request per document.
if _settings.collabora_url:
    _registry.register(
        CollaboraProcessor(
            _settings.collabora_url,
            readers=_readers,
            timeout=_settings.collabora_timeout_seconds,
        ),
        priority=15,
    )

__all__ = [
    "DocumentProcessor",
    "ProcessingResult",
    "ProcessorError",
    "ProcessorRegistry",
    "get_registry",
    "CollaboraProcessor",
    "DocxProcessor",
    "MsgProcessor",
    "PptxProcessor",
    "XlsxProcessor",
    "PyMuPDFProcessor",
    "Pypdfium2FastProcessor",
    "OcrProcessor",
]
