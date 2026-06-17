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
from .classifier import DocClassification, classify_from_text, image_coverage_per_page
from .escalation import TIER_LADDER, EscalationDecision

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

        oversize = self._oversize_result(content, filename, settings)
        if oversize is not None:
            return oversize

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
        # OCR-opted-in tenants only. Shared with the external per-tier path via
        # _classify_result.
        classification = self._classify_result(
            result, content, settings, record=True, filename=filename
        )

        # The tier whose output produced the current ``classification`` -- used as
        # ``from_tier`` for a subsequent OCR hop so a fast->structured->ocr cascade
        # is attributed correctly (the OCR hop is from ``structured``, not a second
        # ``fast`` escalation).
        from_tier = "fast"
        # Set when the structured tier ran but failed to parse. The document is then
        # terminal -- the external path does not escalate a parse FAILURE either --
        # so the OCR gate below must not treat it as a fallback.
        structured_failed = False

        # Escalate a poor fast extraction up the ladder (fast -> structured -> ocr),
        # mirroring the external per-tier path so both modes behave identically. A
        # glyph-corrupt layer (the extractor leaked raw glyph codes -- the
        # broken-/ToUnicode case) OR a low-quality-but-non-empty layer first tries
        # the structured (pymupdf) tier: free, in-cluster, and able to recover both.
        # Only a scanned / no-text-layer doc (total_chars == 0) skips structured --
        # a text extractor cannot conjure text from a pure raster -- and drops
        # straight to OCR via the gate below. Structured is therefore NOT gated on
        # document_ocr_enabled. Its output is re-classified (record=False -- the doc
        # was already counted at the fast tier) so a doc that is ALSO partly scanned
        # still reaches the OCR gate.
        if (
            classification is not None
            and classification.page_count > 0
            and (
                classification.recommended_tier == "structured"
                or (
                    classification.recommended_tier == "ocr"
                    and classification.total_chars > 0
                )
            )
        ):
            structured = self._pdf_processor_for_tier("structured")
            if structured is None:
                # Structured isn't registered: mirror the external
                # next_available_tier, which skips the missing rung and lands on
                # OCR. Leave the recommendation unchanged so the OCR gate below
                # picks it up (incl. a glyph-corrupt "structured" recommendation).
                logger.debug(
                    "No structured processor registered; %s falls through to the "
                    "OCR gate (recommended_tier=%s)",
                    filename or "<bytes>",
                    classification.recommended_tier,
                )
            else:
                reason = (
                    "corrupt_glyphs"
                    if classification.recommended_tier == "structured"
                    else "low_confidence"
                )
                record_document_escalation("fast", "structured", reason)
                logger.info(
                    "Escalating %s fast->structured (reason=%s)",
                    filename or "<bytes>",
                    reason,
                )
                structured_result = await self._run_processor(
                    structured,
                    content,
                    content_type,
                    filename,
                    options,
                    progress_callback,
                    escalated=True,
                )
                if structured_result.success:
                    result = structured_result
                    from_tier = "structured"
                    classification = self._classify_result(
                        result, content, settings, record=False, filename=filename
                    )
                else:
                    structured_failed = True
                    logger.warning(
                        "structured escalation did not succeed for %s (%s); keeping "
                        "the tier-1 result (OCR not attempted)",
                        filename or "<bytes>",
                        structured_result.metadata.get("parse_failed_reason", "error"),
                    )

        # NOTE: the suppressed-escalation metric (document_escalation_suppressed_total,
        # the "what-if OCR" signal; Deck #324) is intentionally NOT emitted on this
        # inline/memory path -- it is instrumented only on the per-tier external
        # path (vector/processor._parse_pdf_tier via evaluate_escalation). When OCR
        # is off here the would-be escalation is simply not taken (the gate below);
        # operators reading the suppressed counter are on the procrastinate fleet.
        #
        # Escalate to OCR (tier-3) when enabled and a provider is registered. Fires
        # for a scanned / no-text-layer doc (recommended "ocr"), and also for an
        # unresolved "structured" recommendation -- a glyph-corrupt doc whose
        # structured rung wasn't registered -- so the inline path falls through to
        # OCR exactly like the external next_available_tier. ``structured_failed``
        # excludes a doc whose structured parse FAILED (terminal, like the external
        # path). Note: a fast FAILURE (result.success False, no classification) is
        # NOT escalated; a PDF pypdfium2 can't open is a hard failure (OCR reads the
        # same bytes and would usually fail too). The page_count guard skips a
        # zero-page (empty/corrupt) PDF, which OCR can't help either.
        if (
            classification is not None
            and not structured_failed
            and classification.recommended_tier in ("ocr", "structured")
            and classification.page_count > 0
            and (
                settings.document_ocr_enabled or settings.document_ocr_incluster_enabled
            )
        ):
            # Inline (memory pool) path: no queues to hop, so pick the cheapest
            # available OCR rung (in-cluster GPU before paid upstream) via the same
            # availability walk the queue path uses.
            ocr_tier = self.next_available_tier(
                from_tier, settings, minimum="ocr-incluster"
            )
            ocr = self._pdf_processor_for_tier(ocr_tier) if ocr_tier else None
            if ocr is not None and ocr_tier is not None:
                reason = (
                    "corrupt_glyphs"
                    if classification.recommended_tier == "structured"
                    else "empty_text"
                    if classification.total_chars == 0
                    else "low_confidence"
                )
                record_document_escalation(from_tier, ocr_tier, reason)
                logger.info(
                    "Escalating %s %s->%s (reason=%s)",
                    filename or "<bytes>",
                    from_tier,
                    ocr_tier,
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
                # who enables OCR without credentials would make scanned docs fail
                # entirely -- strictly worse than off.
                if ocr_result.success:
                    return ocr_result
                logger.warning(
                    "OCR escalation to %s did not succeed for %s (%s); keeping the "
                    "tier-1 result",
                    ocr_tier,
                    filename or "<bytes>",
                    ocr_result.metadata.get("parse_failed_reason", "error"),
                )

        return result

    def _oversize_result(
        self, content: bytes, filename: str | None, settings: Any
    ) -> ProcessingResult | None:
        """Pre-parse size guard, shared by the inline and per-tier paths.

        A pathologically large PDF (e.g. a 42 MB scanned DUDE) burns the OCR
        timeout for 0 chars. Return an explicit ``oversize`` failure so the
        caller marks the placeholder "failed" instead of retrying; 0 disables the
        cap. An explicit ``processor_name`` override (``registry.process``)
        bypasses tiering entirely and is intentionally not size-gated (power-user
        escape hatch). Skipping ``_run_processor`` means the rejection is counted
        on ``astrolabe_document_parse_failed_total{oversize}`` (via
        ``vector/processor.py``) but deliberately not on the parse-duration
        histogram -- there is no parse to time.
        """
        max_pdf_mb = settings.document_max_pdf_size_mb
        if max_pdf_mb > 0 and len(content) > max_pdf_mb * 1024 * 1024:
            size_mb = len(content) / (1024 * 1024)
            logger.warning(
                "PDF %s is %.1f MB (> %.1f MB cap); failing fast as oversize",
                filename or "<bytes>",
                size_mb,
                max_pdf_mb,
            )
            return ProcessingResult(
                text="",
                metadata={"parse_failed_reason": "oversize"},
                processor="size_guard",
                success=False,
                error=(f"PDF exceeds size cap: {size_mb:.1f} MB > {max_pdf_mb:.1f} MB"),
            )
        return None

    def _classify_result(
        self,
        result: ProcessingResult,
        content: bytes,
        settings: Any,
        *,
        record: bool,
        filename: str | None = None,
    ) -> DocClassification | None:
        """Tier-0 classification of a parse result (text-only, cheap).

        Shared by the inline memory-backend pipeline (:meth:`_process_pdf`) and
        the external per-tier path (:meth:`evaluate_escalation`). Returns
        ``None`` when classification is disabled, the parse failed, or the
        classifier raised -- best-effort, a classify failure must never break
        indexing. ``record`` emits the classification metrics; set it only at the
        FIRST classification of a document (the ``fast`` tier) so the per-doc
        counters aren't multiplied across tiers.
        """
        if not (settings.document_classify_enabled and result.success):
            return None
        try:
            image_coverage = None
            # Scan detection feeds either OCR rung (tier2 in-cluster or tier3
            # upstream), so run it whenever EITHER is enabled — a tenant with
            # only in-cluster OCR on still needs image-coverage scan signals.
            ocr_any_enabled = (
                settings.document_ocr_enabled or settings.document_ocr_incluster_enabled
            )
            if ocr_any_enabled and settings.document_ocr_detect_scanned:
                try:
                    image_coverage = image_coverage_per_page(content)
                except Exception:
                    # Best-effort: fall back to text-only signals. WARNING (not
                    # DEBUG) so a systematic scan-detection failure on an
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
                glyph_corruption_ratio=settings.document_glyph_corruption_ratio,
                image_coverage=image_coverage,
            )
        except Exception:
            logger.warning(
                "Tier-0 classification failed for %s",
                filename or "<bytes>",
                exc_info=True,
            )
            return None
        if record:
            record_document_classification(
                classification.recommended_tier,
                classification.flags,
                classification.mean_text_quality,
                classification.ocr_page_fraction,
            )
        return classification

    def _tier_available(
        self, tier: str, settings: Any, *, ignore_ocr_enabled: bool = False
    ) -> bool:
        """Whether ``tier`` can run a PDF parse right now.

        A tier is available when it has a registered PDF processor and is
        enabled; the ``ocr`` tier additionally requires ``DOCUMENT_OCR_ENABLED``
        (so OCR stays opt-in and a misconfigured tenant never escalates to a
        backend it hasn't turned on).

        ``ignore_ocr_enabled`` drops only the OCR-enabled gate (not the registered-
        processor requirement): it answers "would this tier run if OCR were turned
        on?" — used to compute the *ideal* escalation target for the what-if-OCR
        suppressed-escalation signal. (Today only ``ocr`` has an enabled gate; a
        future per-tier gate would extend the condition below.)
        """
        if self._pdf_processor_for_tier(tier) is None:
            return False
        # Each OCR rung has its own opt-in flag (in-cluster vs upstream); a rung is
        # unavailable when its flag is off (unless we're computing the what-if
        # ideal target). Non-OCR tiers have no enabled gate.
        ocr_enable = {
            "ocr-incluster": settings.document_ocr_incluster_enabled,
            "ocr-upstream": settings.document_ocr_enabled,
        }
        if not ignore_ocr_enabled and tier in ocr_enable and not ocr_enable[tier]:
            return False
        return True

    def next_available_tier(
        self,
        current_tier: str,
        settings: Any,
        *,
        minimum: str | None = None,
        ignore_ocr_enabled: bool = False,
    ) -> str | None:
        """First escalation target above ``current_tier`` that can actually run.

        Walks the ladder strictly above ``current_tier`` (and not below
        ``minimum``'s rung, when given) and returns the first
        :meth:`_tier_available` tier. ``None`` means no higher tier can run --
        ``current_tier`` is then terminal and its result is indexed as-is.
        ``ignore_ocr_enabled`` is forwarded to :meth:`_tier_available` to find the
        *ideal* target ignoring the OCR-enabled gate (see ``evaluate_escalation``).
        """
        try:
            cur_idx = TIER_LADDER.index(current_tier)
        except ValueError:
            return None
        start_idx = cur_idx + 1
        if minimum is not None:
            try:
                start_idx = max(start_idx, TIER_LADDER.index(minimum))
            except ValueError:
                pass
        for tier in TIER_LADDER[start_idx:]:
            if self._tier_available(
                tier, settings, ignore_ocr_enabled=ignore_ocr_enabled
            ):
                return tier
        return None

    async def process_tier(
        self,
        content: bytes,
        content_type: str,
        filename: str | None,
        tier: str,
        options: dict[str, Any] | None = None,
        progress_callback: (
            Callable[[float, float | None, str | None], Awaitable[None]] | None
        ) = None,
    ) -> ProcessingResult:
        """Run exactly ONE extraction tier's processor on a PDF (external path).

        The per-tier procrastinate fleet calls this for the tier matching the
        job's queue. Escalation to the next tier is decided separately by
        :meth:`evaluate_escalation` and effected by the queue's retry strategy as
        a queue-hop -- never inline here. ``escalated`` is set for any tier above
        the cheapest so the parse span/metrics reflect an escalated attempt.
        """
        oversize = self._oversize_result(content, filename, get_settings())
        if oversize is not None:
            return oversize
        processor = self._pdf_processor_for_tier(tier)
        if processor is None:
            raise ProcessorError(
                f"No '{tier}'-tier PDF processor registered "
                f"(available: {', '.join(self.list_processors())})"
            )
        return await self._run_processor(
            processor,
            content,
            content_type,
            filename,
            options,
            progress_callback,
            escalated=(tier != TIER_LADDER[0]),
        )

    def evaluate_escalation(
        self,
        result: ProcessingResult,
        content: bytes,
        current_tier: str,
        settings: Any,
        *,
        filename: str | None = None,
    ) -> EscalationDecision | None:
        """Decide whether ``current_tier``'s result must escalate (external path).

        Returns an :class:`EscalationDecision` (``"hop"`` or ``"suppressed"``)
        when the classifier judges the parse too poor to index, else ``None``
        (index as-is). Reuses the tier-0 classifier as the post-parse quality
        gate, so the signal is identical to the inline pipeline's.

        A hard parse FAILURE (``result.success`` False) is never escalated: a
        corrupt/encrypted PDF one engine can't open usually defeats the others
        too (OCR reads the same bytes), so the caller marks it failed instead.

        Target-tier routing:

        - ``total_chars == 0`` (scanned / no text layer) -> target the ``ocr``
          tier directly. Text-extractor tiers (``structured``) cannot conjure
          text from a pure raster scan, so a structured hop would just be wasted.
        - glyph-corrupt text layer (``recommended_tier == "structured"``) -> target
          the ``structured`` tier; pymupdf re-extracts a broken-/ToUnicode layer
          correctly, so OCR is never the target for this case.
        - low-confidence but non-empty layer -> escalate to the next rung, so a
          different in-cluster extractor can try before paying for OCR.

        Hop vs suppressed: if the ideal target tier can run, return a ``"hop"``.
        If it can't run **only because it's disabled** (OCR off — the *ideal*
        tier exists ignoring the enabled gate, but the *available* one does not),
        return ``"suppressed"`` so the caller records the would-be hop and indexes
        the current tier's output as terminal (OCR stays opt-in + cost-free, but
        the latent demand is observable). If no higher tier exists *at all* (no
        processor registered), it's genuinely terminal -> ``None``.
        """
        classification = self._classify_result(
            result,
            content,
            settings,
            record=(current_tier == TIER_LADDER[0]),
            filename=filename,
        )
        if classification is None or classification.recommended_tier not in (
            "structured",
            "ocr",
        ):
            return None
        # A zero-page (empty/corrupt) PDF gains nothing from any tier.
        if classification.page_count <= 0:
            return None
        minimum: str | None
        if classification.recommended_tier == "structured":
            # Glyph-corrupt text layer (the extractor leaked glyph codes): a
            # different in-cluster extractor (the structured/pymupdf tier) recovers
            # it -- never pay for OCR here. Target the structured rung specifically.
            minimum = "structured"
            reason = "corrupt_glyphs"
        elif classification.total_chars == 0:
            # Scanned / no text layer: target the cheapest OCR rung (in-cluster
            # GPU); next_available_tier then falls through to the upstream rung if
            # in-cluster is disabled/unregistered.
            minimum = "ocr-incluster"
            reason = "empty_text"
        else:
            minimum = None
            reason = "low_confidence"
        to_tier = self.next_available_tier(current_tier, settings, minimum=minimum)
        if to_tier is not None:
            return EscalationDecision("hop", to_tier, reason)
        # No tier can run as configured. Distinguish "disabled (e.g. OCR off)"
        # from "no such tier at all" by re-resolving ignoring the enabled gate.
        ideal = self.next_available_tier(
            current_tier, settings, minimum=minimum, ignore_ocr_enabled=True
        )
        if ideal is not None:
            return EscalationDecision("suppressed", ideal, reason)
        return None

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
