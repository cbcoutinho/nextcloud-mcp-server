"""Central registry for document processors."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.observability.metrics import (
    record_document_classification,
    record_document_escalation,
    record_document_parse,
)
from nextcloud_mcp_server.observability.tracing import trace_operation

from .base import DocumentProcessor, ProcessingResult, ProcessorError
from .classifier import classify_from_text, image_coverage_per_page

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
        # Forced processor bypasses tiering.
        if processor_name:
            processor = self.get_processor(processor_name)
            if not processor:
                raise ProcessorError(
                    f"Processor '{processor_name}' not found. "
                    f"Available: {', '.join(self.list_processors())}"
                )
            return await self._run_processor(
                processor, content, content_type, filename, options, progress_callback
            )

        # PDFs go through the tiered pipeline (tier-0 classify -> tier-1 fast ->
        # tier-3 OCR escalation). Everything else uses priority selection.
        if content_type.split(";")[0].strip().lower() == "application/pdf":
            return await self._process_pdf(
                content, content_type, filename, options, progress_callback
            )

        processor = self.find_processor(content_type)
        if not processor:
            raise ProcessorError(
                f"No processor found for type: {content_type}. "
                f"Registered processors: {', '.join(self.list_processors())}"
            )
        return await self._run_processor(
            processor, content, content_type, filename, options, progress_callback
        )

    def _pdf_processor_for_tier(self, tier: str) -> DocumentProcessor | None:
        """First registered processor of ``tier`` that handles PDFs."""
        for name in self._priority_order:
            processor = self._processors[name][0]
            if processor.tier == tier and processor.supports("application/pdf"):
                return processor
        return None

    async def _process_pdf(
        self,
        content: bytes,
        content_type: str,
        filename: str | None,
        options: dict[str, Any] | None,
        progress_callback: (
            Callable[[float, float | None, str | None], Awaitable[None]] | None
        ),
    ) -> ProcessingResult:
        """Tiered PDF pipeline.

        pypdfium2 ``fast`` extracts first; classification is then derived from
        that text (no PDF re-open), and a scanned/no-text-layer doc escalates to
        the ``ocr`` tier when enabled. ``document_tier1_engine="pymupdf"`` is a
        deprecated rollback that pins the structured engine instead.
        """
        settings = get_settings()

        if settings.document_tier1_engine == "pymupdf":
            processor = self._pdf_processor_for_tier("structured")
            if processor is None:
                # The rollback was set to opt OUT of pypdfium2, so falling back
                # to it (the highest-priority PDF processor) silently would
                # defeat that intent -- warn loudly.
                processor = self.find_processor(content_type)
                if processor is None:
                    raise ProcessorError("No PDF processor registered")
                logger.warning(
                    "document_tier1_engine=pymupdf but no 'structured' processor "
                    "is registered; falling back to '%s'",
                    processor.name,
                )
            return await self._run_processor(
                processor, content, content_type, filename, options, progress_callback
            )

        fast = self._pdf_processor_for_tier("fast")
        if fast is None:
            processor = self.find_processor(content_type)
            if processor is None:
                raise ProcessorError("No PDF processor registered")
            return await self._run_processor(
                processor, content, content_type, filename, options, progress_callback
            )

        result = await self._run_processor(
            fast, content, content_type, filename, options, progress_callback
        )

        # Tier-0 classification from the extraction (cheap: text-only, no PDF
        # re-open). Scan detection (image analysis, re-opens the PDF) runs only
        # when OCR + detect_scanned are enabled, so its cost is paid by
        # OCR-opted-in tenants only.
        classification = None
        if settings.document_classify_enabled and result.success:
            try:
                image_coverage = None
                if (
                    settings.document_ocr_enabled
                    and settings.document_ocr_detect_scanned
                ):
                    try:
                        image_coverage = image_coverage_per_page(content)
                    except Exception:
                        # Best-effort: fall back to text-only signals. WARNING
                        # (not DEBUG) so a systematic scan-detection failure on an
                        # OCR-enabled tenant is visible at LOG_LEVEL=INFO.
                        logger.warning(
                            "Scan detection failed for %s; using text-only signals",
                            filename or "<bytes>",
                            exc_info=True,
                        )
                classification = classify_from_text(
                    result.text,
                    result.metadata.get("page_boundaries") or [],
                    min_text_quality=settings.document_ocr_min_text_quality,
                    min_page_chars=settings.document_ocr_min_page_chars,
                    page_fraction=settings.document_ocr_page_fraction,
                    image_coverage=image_coverage,
                )
                record_document_classification(
                    classification.recommended_tier,
                    classification.flags,
                    classification.mean_text_quality,
                    classification.ocr_page_fraction,
                )
            except Exception:
                logger.warning(
                    "Tier-0 classification failed for %s",
                    filename or "<bytes>",
                    exc_info=True,
                )

        # Escalate scanned / no-text-layer PDFs to OCR (tier-3) when enabled and
        # a provider is registered. The fast tier is terminal otherwise. Note: a
        # fast FAILURE (encrypted/corrupt -- result.success False, no
        # classification) is NOT escalated; a PDF pypdfium2 can't open is treated
        # as a hard failure (OCR reads the same bytes and would usually fail
        # too). The page_count guard skips a zero-page (empty/corrupt) PDF, which
        # OCR can't help either.
        if (
            classification is not None
            and classification.recommended_tier == "ocr"
            and classification.page_count > 0
            and settings.document_ocr_enabled
        ):
            ocr = self._pdf_processor_for_tier("ocr")
            if ocr is not None:
                reason = (
                    "empty_text"
                    if classification.total_chars == 0
                    else "low_confidence"
                )
                record_document_escalation("fast", "ocr", reason)
                logger.info(
                    "Escalating %s fast->ocr (reason=%s)",
                    filename or "<bytes>",
                    reason,
                )
                ocr_result = await self._run_processor(
                    ocr,
                    content,
                    content_type,
                    filename,
                    options,
                    progress_callback,
                    escalated=True,
                )
                # OCR is an enhancement, not a gate: if it can't run (no backend
                # configured / API down) or returns nothing, keep the tier-1
                # result rather than failing the document. Otherwise an operator
                # who sets DOCUMENT_OCR_ENABLED=true without credentials would
                # make scanned docs fail entirely -- strictly worse than off.
                if ocr_result.success:
                    return ocr_result
                logger.warning(
                    "OCR escalation did not succeed for %s (%s); keeping the "
                    "tier-1 result",
                    filename or "<bytes>",
                    ocr_result.metadata.get("parse_failed_reason", "error"),
                )

        return result

    async def _run_processor(
        self,
        processor: DocumentProcessor,
        content: bytes,
        content_type: str,
        filename: str | None = None,
        options: dict[str, Any] | None = None,
        progress_callback: (
            Callable[[float, float | None, str | None], Awaitable[None]] | None
        ) = None,
        *,
        escalated: bool = False,
    ) -> ProcessingResult:
        """Run one processor with the per-processor span + parse metrics."""
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

        byte_size = len(content)
        start_time = time.time()
        with trace_operation(
            "document_processor.parse",
            attributes={
                "processor.name": processor.name,
                "processor.tier": tier,
                "mime_type": content_type,
                "byte_size": byte_size,
                "escalated": escalated,
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
            # Record the tier that actually produced this result so downstream
            # (Qdrant payload pipeline_tier, analytics) reflects escalation
            # instead of a hardcoded "fast".
            result.metadata.setdefault("pipeline_tier", tier)
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
