"""Abstract base class for document processing plugins."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .source import DocumentSource

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

    error: str | None = None
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
    def tier(self) -> str:
        """Extraction tier this processor belongs to (escalation ladder).

        Used as the ``tier`` label/attribute in observability so that adding new
        extraction tiers later (docling, OCR, LLM) is purely additive. Vocabulary
        (cheapest first): ``fast`` -> ``structured`` -> ``ocr`` -> ``llm``.

        Defaults to ``"fast"``; override in processors that belong to a higher
        tier.
        """
        return "fast"

    @property
    @abstractmethod
    def supported_mime_types(self) -> set[str]:
        """Set of MIME types this processor can handle.

        Examples: {"application/pdf", "image/jpeg", "image/png"}
        """
        pass

    async def process_source(
        self,
        source: "DocumentSource",
        options: dict[str, Any] | None = None,
        progress_callback: Callable[[float, float | None, str | None], Awaitable[None]]
        | None = None,
    ) -> ProcessingResult:
        """Process a document from a file-backed handle.

        Concrete on purpose: the default materialises the source and delegates to
        :meth:`process`, so every existing processor keeps working untouched and
        each can be migrated to a path-based parse independently. Override it
        where opening by path avoids holding the whole document in memory (the
        PDF engines); leave it alone where the processor genuinely needs bytes
        (OCR base64, HTTP upload backends).

        Note the default is where peak memory still scales with document size --
        ``read_bytes`` is deliberately greppable for that reason.

        The read is offloaded to a worker thread: for a spooled source it is a
        synchronous disk read of the whole document, and every processor except
        the two PDF engines relies on this default, so once streaming downloads
        are wired in a large non-PDF document would otherwise block the shared
        event loop for the full read.

        Ownership: the CALLER owns ``source`` and is responsible for its
        ``cleanup()``. Implementations must not clean up a source they were
        handed -- they do not know whether the caller still needs it (e.g. for a
        subsequent tier or bbox extraction). The bytes-based ``process()``
        wrappers create their own :class:`MemoryDocumentSource` and clean it up
        themselves.
        """
        from anyio.to_thread import run_sync  # noqa: PLC0415 -- keep imports light

        content = await run_sync(source.read_bytes)
        return await self.process(
            content,
            source.content_type,
            source.filename,
            options,
            progress_callback,
        )

    @abstractmethod
    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: str | None = None,
        options: dict[str, Any] | None = None,
        progress_callback: Callable[[float, float | None, str | None], Awaitable[None]]
        | None = None,
    ) -> ProcessingResult:
        """Process a document and extract text.

        Args:
            content: Document bytes
            content_type: MIME type of the document
            filename: Optional filename for format detection
            options: Processor-specific options (e.g., OCR language, strategy)
            progress_callback: Optional async callback for progress updates.
                Called as: await progress_callback(progress, total, message)
                - progress: Current progress value (monotonically increasing)
                - total: Optional total value (None if unknown)
                - message: Optional human-readable status message

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
