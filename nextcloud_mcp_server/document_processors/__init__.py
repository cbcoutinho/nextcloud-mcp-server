"""Document processing plugins for extracting text from various file formats."""

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .registry import ProcessorRegistry, get_registry

__all__ = [
    "DocumentProcessor",
    "ProcessingResult",
    "ProcessorError",
    "ProcessorRegistry",
    "get_registry",
]
