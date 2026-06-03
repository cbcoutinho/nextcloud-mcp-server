"""Central registry for document processors."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nextcloud_mcp_server.observability.metrics import record_document_parse
from nextcloud_mcp_server.observability.tracing import trace_operation

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
            logger.warning("Processor '%s' already registered, replacing", name)

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
            "Registered processor: %s (priority=%s, supports=%s types)",
            name,
            priority,
            len(processor.supported_mime_types),
        )

    def get_processor(self, name: str) -> DocumentProcessor | None:
        """Get a processor by name.

        Args:
            name: Processor name

        Returns:
            DocumentProcessor instance or None if not found
        """
        if name in self._processors:
            return self._processors[name][0]
        return None

    def find_processor(self, content_type: str) -> DocumentProcessor | None:
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
                logger.debug("Found processor '%s' for type '%s'", name, content_type)
                return processor

        logger.debug("No processor found for type '%s'", content_type)
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
        filename: str | None = None,
        processor_name: str | None = None,
        options: dict[str, Any] | None = None,
        progress_callback: (
            Callable[[float, float | None, str | None], Awaitable[None]] | None
        ) = None,
    ) -> ProcessingResult:
        """Process a document using available processors.

        Args:
            content: Document bytes
            content_type: MIME type
            filename: Optional filename for format detection
            processor_name: Force specific processor (or None for auto-select)
            options: Processing options passed to processor
            progress_callback: Optional async callback for progress updates

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

        tier = processor.tier
        logger.info(
            "Processing with '%s' processor",
            processor.name,
            extra={
                "processor": processor.name,
                "tier": tier,
                "mime_type": content_type,
            },
        )

        # Process (instrumented: per-processor span + parse metrics).
        # NOTE: when the tiered pipeline (docling/OCR/LLM) lands, escalation
        # decisions are recorded here via record_document_escalation() and an
        # add_span_event("document.escalation", ...) -- the escalated=False
        # attribute and the metric are wired ahead of that.
        byte_size = len(content)
        start_time = time.time()
        with trace_operation(
            "document_processor.parse",
            attributes={
                "processor.name": processor.name,
                "processor.tier": tier,
                "mime_type": content_type,
                "byte_size": byte_size,
                "escalated": False,
            },
            record_exception=True,
        ) as span:
            try:
                result = await processor.process(
                    content, content_type, filename, options, progress_callback
                )
            except Exception:
                duration = time.time() - start_time
                record_document_parse(
                    processor.name,
                    tier,
                    duration,
                    byte_size=byte_size,
                    status="error",
                )
                # Structured error signal for Loki (the processor logs the
                # traceback; this adds the aggregatable fields). The span
                # records the exception itself via record_exception=True.
                logger.warning(
                    "Parse failed for %s with '%s' after %.2fs",
                    filename or "<bytes>",
                    processor.name,
                    duration,
                    extra={
                        "processor": processor.name,
                        "tier": tier,
                        "byte_size": byte_size,
                        "duration_ms": round(duration * 1000, 1),
                        "status": "error",
                    },
                )
                raise

            duration = time.time() - start_time
            pages = int(result.metadata.get("page_count", 0) or 0)
            chars = len(result.text)
            status = "success" if result.success else "error"
            record_document_parse(
                processor.name,
                tier,
                duration,
                pages=pages,
                chars=chars,
                byte_size=byte_size,
                status=status,
            )
            if span is not None:
                span.set_attribute("page_count", pages)
                span.set_attribute("char_count", chars)
                span.set_attribute("processor.success", result.success)

            logger.info(
                "Parsed %s with '%s': %s pages, %s chars in %.2fs",
                filename or "<bytes>",
                processor.name,
                pages,
                chars,
                duration,
                extra={
                    "processor": processor.name,
                    "tier": tier,
                    "pages": pages,
                    "chars": chars,
                    "byte_size": byte_size,
                    "duration_ms": round(duration * 1000, 1),
                    "status": status,
                },
            )
            return result


# Global registry instance
_registry = ProcessorRegistry()


def get_registry() -> ProcessorRegistry:
    """Get the global processor registry.

    Returns:
        Singleton ProcessorRegistry instance
    """
    return _registry
