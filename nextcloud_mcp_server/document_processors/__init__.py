"""Document processing plugins for extracting text from various file formats."""

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .ocr import OcrProcessor
from .pymupdf import PyMuPDFProcessor
from .pypdfium2_fast import Pypdfium2FastProcessor
from .registry import ProcessorRegistry, get_registry

# Register processors at module initialization. The tiered PDF pipeline selects
# by tier (not priority): Pypdfium2FastProcessor is the ``fast`` tier,
# PyMuPDFProcessor the ``structured`` rollback, and TWO OcrProcessor instances are
# the OCR rungs — ``ocr-incluster`` (the on-demand burst GPU, gateway-only, reached
# via the embedding gateway over the tailnet; e.g. surya) tried before
# ``ocr-upstream`` (paid Mistral). Each is reached only when its own opt-in flag is
# set. OCR gets the lowest priorities so it's never the non-tiered default for PDFs.
_registry = get_registry()
_registry.register(Pypdfium2FastProcessor(), priority=20)
_registry.register(PyMuPDFProcessor(), priority=10)
_registry.register(
    OcrProcessor(
        name="ocr-incluster",
        tier="ocr-incluster",
        model_setting="document_ocr_incluster_model",
        gateway_only=True,
    ),
    priority=2,
)
_registry.register(
    OcrProcessor(
        name="ocr-upstream",
        tier="ocr-upstream",
        model_setting="document_ocr_model",
        gateway_only=False,
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
