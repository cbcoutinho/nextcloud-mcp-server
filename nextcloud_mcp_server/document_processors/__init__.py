"""Document processing plugins for extracting text from various file formats."""

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .pymupdf import PyMuPDFProcessor
from .registry import ProcessorRegistry, get_registry

# Register processors at module initialization
_registry = get_registry()
_registry.register(PyMuPDFProcessor(), priority=10)

__all__ = [
    "DocumentProcessor",
    "ProcessingResult",
    "ProcessorError",
    "ProcessorRegistry",
    "get_registry",
    "PyMuPDFProcessor",
]
