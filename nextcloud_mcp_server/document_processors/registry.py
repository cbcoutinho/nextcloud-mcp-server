"""Central registry for document processors."""

import logging
from typing import Any, Optional

from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)


class ProcessorRegistry:
    """Central registry for document processors.

    Manages registration and routing of document processing requests to
    appropriate processors based on MIME types and priorities.

    Example:
        registry = ProcessorRegistry()
        registry.register(UnstructuredProcessor(...), priority=10)
        registry.register(TesseractProcessor(...), priority=5)

        # Auto-select processor based on MIME type
        result = await registry.process(pdf_bytes, "application/pdf")

        # Force specific processor
        result = await registry.process(img_bytes, "image/png", processor_name="tesseract")
    """

    def __init__(self):
        self._processors: dict[str, tuple[DocumentProcessor, int]] = {}
        self._priority_order: list[str] = []

    def register(self, processor: DocumentProcessor, priority: int = 0):
        """Register a document processor.

        Args:
            processor: Processor instance to register
            priority: Higher priority processors are tried first (default: 0)
        """
        name = processor.name

        if name in self._processors:
            logger.warning(f"Processor '{name}' already registered, replacing")

        self._processors[name] = (processor, priority)

        # Update priority order
        if name in self._priority_order:
            self._priority_order.remove(name)

        # Insert in priority order (higher priority first)
        inserted = False
        for i, existing_name in enumerate(self._priority_order):
            existing_priority = self._processors[existing_name][1]
            if priority > existing_priority:
                self._priority_order.insert(i, name)
                inserted = True
                break

        if not inserted:
            self._priority_order.append(name)

        logger.info(
            f"Registered processor: {name} "
            f"(priority={priority}, supports={len(processor.supported_mime_types)} types)"
        )

    def get_processor(self, name: str) -> Optional[DocumentProcessor]:
        """Get a processor by name.

        Args:
            name: Processor name

        Returns:
            DocumentProcessor instance or None if not found
        """
        if name in self._processors:
            return self._processors[name][0]
        return None

    def find_processor(self, content_type: str) -> Optional[DocumentProcessor]:
        """Find the first processor that supports the given MIME type.

        Processors are checked in priority order (highest priority first).

        Args:
            content_type: MIME type to match

        Returns:
            First matching processor or None
        """
        for name in self._priority_order:
            processor = self._processors[name][0]
            if processor.supports(content_type):
                logger.debug(f"Found processor '{name}' for type '{content_type}'")
                return processor

        logger.debug(f"No processor found for type '{content_type}'")
        return None

    def list_processors(self) -> list[str]:
        """List all registered processor names in priority order.

        Returns:
            List of processor names (highest priority first)
        """
        return list(self._priority_order)

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str] = None,
        processor_name: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> ProcessingResult:
        """Process a document using available processors.

        Args:
            content: Document bytes
            content_type: MIME type
            filename: Optional filename for format detection
            processor_name: Force specific processor (or None for auto-select)
            options: Processing options passed to processor

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            ProcessorError: If no processor found or processing fails
        """
        # Find processor
        if processor_name:
            processor = self.get_processor(processor_name)
            if not processor:
                raise ProcessorError(
                    f"Processor '{processor_name}' not found. "
                    f"Available: {', '.join(self.list_processors())}"
                )
        else:
            processor = self.find_processor(content_type)
            if not processor:
                raise ProcessorError(
                    f"No processor found for type: {content_type}. "
                    f"Registered processors: {', '.join(self.list_processors())}"
                )

        logger.info(f"Processing with '{processor.name}' processor")

        # Process
        return await processor.process(content, content_type, filename, options)


# Global registry instance
_registry = ProcessorRegistry()


def get_registry() -> ProcessorRegistry:
    """Get the global processor registry.

    Returns:
        Singleton ProcessorRegistry instance
    """
    return _registry
