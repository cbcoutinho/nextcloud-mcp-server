"""Abstract base class for document processing plugins."""

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel


class ProcessingResult(BaseModel):
    """Standardized result from any document processor."""

    text: str
    """Extracted text content"""

    metadata: dict[str, Any]
    """Processor-specific metadata"""

    processor: str
    """Name of processor that handled this (e.g., 'unstructured', 'tesseract')"""

    success: bool = True
    """Whether processing succeeded"""

    error: Optional[str] = None
    """Error message if processing failed"""


class DocumentProcessor(ABC):
    """Abstract base class for document processing plugins.

    Document processors extract text from various file formats (PDF, DOCX, images, etc.).
    Each processor implements this interface and can be registered with the ProcessorRegistry.

    Example:
        class MyProcessor(DocumentProcessor):
            @property
            def name(self) -> str:
                return "my_processor"

            @property
            def supported_mime_types(self) -> set[str]:
                return {"application/pdf", "image/jpeg"}

            async def process(self, content: bytes, content_type: str, **kwargs) -> ProcessingResult:
                # Extract text from content
                return ProcessingResult(text="...", metadata={}, processor=self.name)

            async def health_check(self) -> bool:
                return True
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this processor (e.g., 'unstructured', 'tesseract')."""
        pass

    @property
    @abstractmethod
    def supported_mime_types(self) -> set[str]:
        """Set of MIME types this processor can handle.

        Examples: {"application/pdf", "image/jpeg", "image/png"}
        """
        pass

    @abstractmethod
    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> ProcessingResult:
        """Process a document and extract text.

        Args:
            content: Document bytes
            content_type: MIME type of the document
            filename: Optional filename for format detection
            options: Processor-specific options (e.g., OCR language, strategy)

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            ProcessorError: If processing fails
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if processor is available and healthy.

        Returns:
            True if processor is ready to use, False otherwise
        """
        pass

    def supports(self, content_type: str) -> bool:
        """Check if this processor supports the given MIME type.

        Args:
            content_type: MIME type (may include parameters like "application/pdf; charset=utf-8")

        Returns:
            True if this processor can handle the type
        """
        # Strip parameters from content type
        base_type = content_type.split(";")[0].strip().lower()
        return base_type in self.supported_mime_types


class ProcessorError(Exception):
    """Raised when document processing fails."""

    pass
