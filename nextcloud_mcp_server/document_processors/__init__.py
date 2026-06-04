"""Document processing plugins for extracting text from various file formats."""

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .pymupdf import PyMuPDFProcessor
from .pypdfium2_fast import Pypdfium2FastProcessor
from .registry import ProcessorRegistry, get_registry

# Register processors at module initialization. The tiered PDF pipeline selects
# by tier (not priority): Pypdfium2FastProcessor is the ``fast`` tier and
# PyMuPDFProcessor the ``structured`` escalation target. Priority still orders
# the non-tiered fallback path and other MIME types.
_registry = get_registry()
_registry.register(Pypdfium2FastProcessor(), priority=20)
_registry.register(PyMuPDFProcessor(), priority=10)

__all__ = [
    "DocumentProcessor",
    "ProcessingResult",
    "ProcessorError",
    "ProcessorRegistry",
    "get_registry",
    "PyMuPDFProcessor",
    "Pypdfium2FastProcessor",
]
