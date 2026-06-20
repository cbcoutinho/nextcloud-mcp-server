"""Document processing plugins for extracting text from various file formats."""

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .ocr import OcrProcessor
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
_registry = get_registry()
_registry.register(Pypdfium2FastProcessor(), priority=20)
_registry.register(PyMuPDFProcessor(), priority=10)
_registry.register(
    OcrProcessor(
        name="ocr",
        tier="ocr",
        model_setting="document_ocr_model",
    ),
    priority=1,
)

__all__ = [
    "DocumentProcessor",
    "ProcessingResult",
    "ProcessorError",
    "ProcessorRegistry",
    "get_registry",
    "PyMuPDFProcessor",
    "Pypdfium2FastProcessor",
    "OcrProcessor",
]
