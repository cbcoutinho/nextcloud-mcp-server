"""Document parsing utilities using the pluggable processor registry.

Two jobs live here:

* :func:`parse_document_source` runs the tiered pipeline against a document that
  is already on disk, and
* :func:`summarize_parse` turns the resulting :class:`ProcessingResult` into the
  handful of plain statements a caller needs to describe what it actually got.

The second exists because a parse can degrade in half a dozen ways that all look
identical from the outside -- a scanned PDF on a tenant without OCR, a
600-page document past the markdown ceiling, an oversize file the guard
rejected, a timeout -- and returning text without saying which of those happened
is how a caller ends up presenting a partial extraction as the whole document.
Every one of those statements is written here, once, so the wording cannot drift
between call sites.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from nextcloud_mcp_server.document_processors import (
    ProcessingResult,
    ProcessorError,
    get_registry,
)
from nextcloud_mcp_server.document_processors.source import DocumentSource
from nextcloud_mcp_server.models.webdav import ContentFormat, ParseStatus

logger = logging.getLogger(__name__)


def is_parseable_document(content_type: Optional[str]) -> bool:
    """Whether any registered processor can extract text from this type.

    There is no instance-wide "document processing" switch: whether to parse a
    document on read is the caller's decision (``nc_webdav_read_file``'s
    ``parse_document`` argument). This answers only "is there a processor for
    this MIME type", which the caller cannot know.
    """
    if not content_type:
        return False

    registry = get_registry()
    return registry.find_processor(content_type) is not None


async def parse_document_source(
    source: DocumentSource,
    *,
    prefer_markdown: bool = False,
    progress_callback: Optional[
        Callable[[float, Optional[float], Optional[str]], Awaitable[None]]
    ] = None,
) -> ProcessingResult:
    """Run the tiered pipeline against a document that is already on disk.

    Source-based on purpose: the interactive read path streams the document to a
    spool file rather than buffering it, so peak memory stays at one chunk no
    matter how large the document is. Handing the *source* down (rather than its
    bytes) is what keeps that true through the parse -- both PDF engines open the
    path natively.

    ``prefer_markdown`` asks the pipeline for reconstructed structure rather than
    a flat text layer; it is bounded by ``document_markdown_max_pages`` and is
    recorded on the result when it could not be honoured.

    Never raises for a processor-level failure: a failed parse comes back as a
    ``ProcessingResult`` with ``success=False`` and a ``parse_failed_reason``, so
    the caller can say what went wrong instead of guessing from an exception.
    """
    registry = get_registry()
    options = {"prefer_markdown": True} if prefer_markdown else None

    logger.debug(
        "Parsing document of type '%s'%s",
        source.content_type,
        " (markdown requested)" if prefer_markdown else "",
    )

    try:
        result = await registry.process_source(
            source, options=options, progress_callback=progress_callback
        )
    except ProcessorError as e:
        logger.warning("Document processing failed: %s", e)
        return ProcessingResult(
            text="",
            metadata={"parse_failed_reason": "error"},
            processor="unknown",
            success=False,
            error=str(e),
        )

    logger.info(
        "Parsed document with '%s' processor (success=%s)",
        result.processor,
        result.success,
    )
    return result


@dataclass
class ParseSummary:
    """What a parse actually produced, in terms a caller can report verbatim."""

    status: ParseStatus
    tier: str | None = None
    processor: str | None = None
    content_format: ContentFormat = "text"
    notes: list[str] = field(default_factory=list)


def summarize_parse(result: ProcessingResult, settings: Any) -> ParseSummary:
    """Describe a :class:`ProcessingResult` honestly.

    Pure: no I/O, no globals beyond the ``settings`` handed in, so every
    degradation path is unit-testable without a running pipeline.
    """
    metadata = result.metadata or {}
    tier = metadata.get("pipeline_tier")
    notes: list[str] = []

    if not result.success:
        # A failed parse is never dressed up as content. The tier/processor are
        # still reported so the caller can see what was attempted.
        reason = metadata.get("parse_failed_reason", "error")
        if result.processor == "size_guard" or reason == "oversize":
            notes.append(
                f"The document was not parsed: it exceeds the "
                f"{settings.document_max_pdf_size_mb:g} MB parse cap "
                f"(DOCUMENT_MAX_PDF_SIZE_MB)."
            )
        else:
            where = f" in the '{tier}' tier" if tier else ""
            notes.append(f"Parsing failed ({reason}){where}; no text was extracted.")
        return ParseSummary(
            status="failed",
            tier=tier,
            processor=result.processor,
            content_format="text",
            notes=notes,
        )

    content_format: ContentFormat = (
        "markdown" if metadata.get("parse_mode") == "markdown" else "text"
    )

    skipped = metadata.get("markdown_skipped_reason")
    if skipped == "page_ceiling":
        pages = metadata.get("page_count")
        notes.append(
            f"Markdown structure was not reconstructed: this document has "
            f"{pages} pages, above DOCUMENT_MARKDOWN_MAX_PAGES="
            f"{settings.document_markdown_max_pages}. The raw per-page text layer "
            f"is returned instead."
        )
    elif skipped == "disabled":
        notes.append(
            "Markdown reconstruction is switched off on this server "
            "(DOCUMENT_MARKDOWN_MAX_PAGES=0); the raw text layer is returned."
        )
    elif skipped == "not_registered":
        notes.append(
            "No structured-parse engine is available on this server, so the raw "
            "text layer is returned without markdown structure."
        )
    elif skipped == "parse_failed":
        notes.append(
            "Markdown reconstruction was attempted and failed; the raw text layer "
            "is returned instead."
        )

    ocr_skipped = metadata.get("ocr_escalation_skipped")
    if ocr_skipped == "disabled":
        notes.append(
            "This document has little or no usable text layer and OCR is not "
            "enabled on this server (DOCUMENT_OCR_ENABLED), so the text below is "
            "only what a text extractor could recover -- it may be incomplete or "
            "empty."
        )
    elif ocr_skipped == "not_registered":
        notes.append(
            "This document needs OCR, but no OCR backend is configured on this "
            "server (DOCUMENT_OCR_PROVIDER); the text below may be incomplete or "
            "empty."
        )
    elif metadata.get("ocr_escalation_failed"):
        notes.append(
            f"OCR was attempted and did not succeed "
            f"({metadata['ocr_escalation_failed']}); the text below is what a text "
            f"extractor could recover."
        )

    if not result.text:
        notes.append("The parse succeeded but extracted 0 characters of text.")

    return ParseSummary(
        status="parsed",
        tier=tier,
        processor=result.processor,
        content_format=content_format,
        notes=notes,
    )
