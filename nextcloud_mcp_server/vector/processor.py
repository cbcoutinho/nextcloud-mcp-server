"""Processor task for vector database synchronization.

Processes documents from stream: fetches content, generates embeddings, stores in Qdrant.
"""

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

import anyio
import httpx
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream
from qdrant_client.models import PointStruct

if TYPE_CHECKING:
    # Type-only: the document stack is heavy (pymupdf/_isolation) and must stay
    # off processor.py's import path (#877); the runtime import is lazy.
    from nextcloud_mcp_server.document_processors.base import ProcessingResult
    from nextcloud_mcp_server.document_processors.registry import ProcessorRegistry

from contextlib import AsyncExitStack

from nextcloud_mcp_server.acl_hash import compute_acl_hash
from nextcloud_mcp_server.capabilities import allowed_doc_types, is_doc_type_allowed
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.document_processors.source import (
    DocumentSource,
    MemoryDocumentSource,
)
from nextcloud_mcp_server.embedding import get_bm25_service, get_embedding_service
from nextcloud_mcp_server.models.deck import DeckCard
from nextcloud_mcp_server.observability.metrics import (
    estimate_vector_bytes,
    record_chunk_density,
    record_document_chunks,
    record_document_dead_lettered,
    record_document_escalation,
    record_document_escalation_suppressed,
    record_document_ingest_rejected,
    record_document_ingest_size,
    record_document_parse_failed,
    record_embedding,
    record_embedding_tokens,
    record_estimated_vector_bytes,
    record_ingest_dropped,
    record_qdrant_operation,
    record_vector_sync_processing,
    update_vector_sync_queue_size,
)
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.search.pdf_highlighter import PDFHighlighter
from nextcloud_mcp_server.usage import UsageEvent, UsageEventStore
from nextcloud_mcp_server.utils.validation import is_valid_nextcloud_doc_id
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector._errors import format_exception_group
from nextcloud_mcp_server.vector.collection_metadata import build_embedding_identity
from nextcloud_mcp_server.vector.dead_letter import (
    clear_dead_letter,
    mark_dead_letter,
)
from nextcloud_mcp_server.vector.document_chunker import (
    ChunkWithPosition,
    DocumentChunker,
    PageAwareChunker,
)
from nextcloud_mcp_server.vector.html_processor import html_to_markdown
from nextcloud_mcp_server.vector.mail_content import (
    build_mail_content,
    format_mail_addresses,
)
from nextcloud_mcp_server.vector.placeholder import (
    delete_placeholder_point,
    update_placeholder_status,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client
from nextcloud_mcp_server.vector.scanner import DocumentTask, _discover_tagged_files
from nextcloud_mcp_server.vector.sharing_state import (
    claim_existing_index,
    existing_principals,
    file_title_from_path,
    release_document_for_user,
)
from nextcloud_mcp_server.vector.spool import spooled_document

logger = logging.getLogger(__name__)

# Shared span-attribute key (avoids duplicating the string literal across the
# many vector_sync spans that report a chunk count).
_ATTR_CHUNK_COUNT = "vector_sync.chunk_count"


def _drop_reason(exc: BaseException) -> str:
    """Classify a terminal indexing failure into a metric label.

    Distinguishes the transient backend-pod-rollover causes (connection /
    timeout — the ones provider-level retry should now ride through, card 309)
    from persistent faults, so ``astrolabe_vector_ingest_dropped_total`` is
    alertable per cause. Descends through nested ExceptionGroups to the first
    leaf so a doubly-wrapped cause isn't mislabelled ``other``. Best-effort:
    unknown causes fall back to ``other``.
    """
    # An anyio task group can wrap the real cause (and nest groups when sub-tasks
    # use their own groups); descend to the first concrete leaf. Best-effort: a
    # group bundling several distinct failures is labelled by whichever leaf
    # sorts first, not by a "mixed" bucket.
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]

    # Raw httpx transport errors from direct Nextcloud API calls (the nc_client
    # uses httpx directly); the openai checks below catch the SDK-wrapped
    # variants of the same failure modes.
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection"

    # openai.* is always installed (provider dep) but import lazily to keep this
    # helper cheap and decoupled from a specific SDK version's surface.
    try:
        import openai  # noqa: PLC0415

        if isinstance(exc, openai.APITimeoutError):
            return "timeout"
        if isinstance(exc, openai.APIConnectionError):
            return "connection"
        if isinstance(exc, openai.RateLimitError):
            return "rate_limit"
        if isinstance(exc, openai.APIStatusError):
            return "server" if exc.status_code >= 500 else "other"
    except ImportError:  # pragma: no cover — openai is a hard dependency
        pass

    # Qdrant client errors surface from its own module namespace.
    if type(exc).__module__.startswith("qdrant_client"):
        return "qdrant"
    return "other"


PDF_MIME_TYPE = "application/pdf"


def _is_pdf(content_type: str) -> bool:
    """Whether a MIME type is a PDF (parameter-tolerant)."""
    return content_type.split(";")[0].strip().lower() == PDF_MIME_TYPE


async def _parse_pdf_tier(
    registry: "ProcessorRegistry",
    source: DocumentSource,
    tier: str,
    settings: Any,
    options: dict[str, Any] | None = None,
) -> "ProcessingResult":
    """Run a single extraction tier and apply the post-parse escalation gate.

    The external per-tier ingest path (Deck #323): the procrastinate worker for
    ``tier`` parses with exactly that tier, then either returns the result to
    index or raises ``EscalateError`` to hand the document to the next tier's
    queue (the queue's retry strategy turns the raise into a native queue-hop).
    The escalation metric is recorded here, at the decision point.

    A hard parse failure (``result.success`` False) is returned as-is, not
    escalated -- a corrupt/encrypted/oversize PDF that one engine can't open
    usually defeats the others too; the caller marks it failed. This preserves
    the "OCR is an enhancement, never worse than off" invariant: a tenant who has
    not enabled a higher tier (or has no processor for it) simply indexes the
    cheap tier's output.

    Batch OCR (Deck #332): when the OCR tier's batch job is still in flight the
    processor returns a *pending sentinel* result; we translate it here into a
    ``BatchPending`` raise (same decision point as ``EscalateError``) so the
    retry strategy re-runs this tier after a delay instead of indexing empty text.
    """
    # Lazy import: keep the document stack (pymupdf/_isolation) off the module
    # load path; this runs only on the per-tier worker, which needs it anyway.
    from nextcloud_mcp_server.document_processors.escalation import (  # noqa: PLC0415
        BatchPending,
        EscalateError,
    )
    from nextcloud_mcp_server.document_processors.ocr import (  # noqa: PLC0415
        OCR_BATCH_PENDING_KEY,
        OCR_BATCH_RETRY_IN_KEY,
    )

    # ``options`` threads per-document identity (user_id/doc_id/doc_type/etag) to
    # the OCR tier so batch mode can key its job-tracking table (Deck #332). Other
    # tiers ignore it. The inline path (registry.process) passes None.
    filename = source.filename
    result = await registry.process_tier_source(source, tier, options=options)
    if result.metadata.get(OCR_BATCH_PENDING_KEY):
        raise BatchPending(retry_in=int(result.metadata[OCR_BATCH_RETRY_IN_KEY]))
    if result.success:
        decision = registry.evaluate_escalation_source(result, source, tier, settings)
        if decision is not None:
            if decision.kind == "suppressed":
                # The ideal next tier (e.g. ocr) is disabled, so we do NOT hop:
                # index this tier's output as terminal and record the would-be
                # escalation so operators see the latent demand ("what-if OCR
                # enabled"; #324).
                record_document_escalation_suppressed(
                    tier, decision.to_tier, decision.reason
                )
                logger.info(
                    "Escalation suppressed for %s: %s->%s disabled (reason=%s), "
                    "indexing at current tier",
                    filename or "<bytes>",
                    tier,
                    decision.to_tier,
                    decision.reason,
                )
            else:  # "hop" — the Literal kind makes this branch exhaustive.
                record_document_escalation(tier, decision.to_tier, decision.reason)
                logger.info(
                    "Escalating %s %s->%s (reason=%s)",
                    filename or "<bytes>",
                    tier,
                    decision.to_tier,
                    decision.reason,
                )
                raise EscalateError(
                    from_tier=tier, to_tier=decision.to_tier, reason=decision.reason
                )
    return result


def _ocr_chunk_bboxes(
    chunks: list[ChunkWithPosition], block_spans: list[dict[str, Any]]
) -> dict[int, list[tuple[float, float, float, float]]]:
    """Attribute OCR block bboxes to chunks by char-span overlap.

    ``block_spans`` is the OCR tier's per-block geometry (``OCR_BLOCK_SPANS_KEY``):
    each ``{"bbox": [x0,y0,x1,y1] normalized, "start_offset", "end_offset",
    "page", ...}``. A block belongs to chunk *i* when their char spans intersect
    (half-open overlap: ``block.start < chunk.end and block.end > chunk.start``)
    AND the block's page matches the chunk's ``page_number``.

    The page guard makes attribution correct-by-construction independent of the
    chunker. ``chunk_bbox`` is stored as a flat rect list rendered on the chunk's
    single ``page_number``, so a block from a *different* page must not be
    attributed — its box would render on the wrong page at the wrong page's
    coordinates. The page-aware chunker keeps every chunk single-page (guard is a
    no-op), but the page-agnostic char-based chunker can straddle a page break: on
    Student 147, 9/16 char-based chunks pulled blocks from two pages. When a chunk
    has no ``page_number`` (no page boundaries), fall back to overlap-only.
    Conversely, a span without a ``page`` key is excluded from any chunk that has a
    ``page_number`` — ``_pages_to_text`` always stamps ``page`` on OCR spans, so this
    only affects externally-built spans.

    Returns ``chunk_index -> [bbox, ...]`` (a chunk spanning N blocks gets N bboxes,
    in the spans' reading order), omitting chunks with no overlapping block. Pure +
    module-level so the predicate is unit-testable in isolation."""
    out: dict[int, list[tuple[float, float, float, float]]] = {}
    for i, chunk in enumerate(chunks):
        page = getattr(chunk, "page_number", None)
        boxes = [
            (s["bbox"][0], s["bbox"][1], s["bbox"][2], s["bbox"][3])
            for s in block_spans
            if s["start_offset"] < chunk.end_offset
            and s["end_offset"] > chunk.start_offset
            and (page is None or s.get("page") == page)
        ]
        if boxes:
            out[i] = boxes
    return out


def assign_page_numbers(chunks, page_boundaries):
    """Assign page numbers to chunks based on page boundaries.

    Each chunk gets the page number where most of its content appears.
    For chunks spanning multiple pages, assigns the page containing the
    majority of the chunk's characters.

    Args:
        chunks: List of ChunkWithPosition objects
        page_boundaries: List of dicts with {page, start_offset, end_offset}

    Returns:
        None (modifies chunks in place)
    """
    if not page_boundaries:
        return

    for chunk in chunks:
        # Find which page(s) this chunk overlaps with
        max_overlap = 0
        assigned_page = None

        for boundary in page_boundaries:
            # Calculate overlap between chunk and page
            overlap_start = max(chunk.start_offset, boundary["start_offset"])
            overlap_end = min(chunk.end_offset, boundary["end_offset"])
            overlap = max(0, overlap_end - overlap_start)

            # Assign to page with maximum overlap
            if overlap > max_overlap:
                max_overlap = overlap
                assigned_page = boundary["page"]

        if assigned_page is not None:
            chunk.page_number = assigned_page


def resolve_page_end(chunk: ChunkWithPosition) -> int | None:
    """Citation end-page for a chunk's Qdrant payload (Deck #636).

    Packed multi-page chunks carry a real ``page_end`` (last page of the range);
    every other chunk leaves it ``None`` — notably the char-based path, where
    :func:`assign_page_numbers` back-fills ``page_number`` post-hoc but never
    ``page_end``. Fall back to ``page_number`` so the payload always ships a
    citation range (single-page chunks report ``page_end == page_number``).
    """
    return chunk.page_end if chunk.page_end is not None else chunk.page_number


def should_use_page_aware(
    *, page_aware_enabled: bool, doc_type: str, page_boundaries: Any
) -> bool:
    """Decide whether the page-aware chunker applies to this document.

    Page-aware chunking applies only to paginated files (PDFs) that actually
    carry page boundaries. ``page_boundaries`` is tested for truthiness, not
    just ``is not None``: an empty list carries no pages, so it routes through
    the char-based path rather than the page-aware chunker's no-boundaries
    fallback.

    Args:
        page_aware_enabled: ``settings.document_chunk_page_aware``.
        doc_type: The document type (only ``"file"`` is paginated).
        page_boundaries: The extractor's page-boundary list (or ``None``).
    """
    return page_aware_enabled and doc_type == "file" and bool(page_boundaries)


def _download_ceiling(settings: Any) -> int | None:
    """Hard byte ceiling for a streamed download, or ``None`` when uncapped.

    The pre-flight gate acts on the size the server advertised at scan time;
    this acts on what actually arrives, so it still holds when Content-Length is
    absent or wrong. Sized generously above the parse cap -- its job is to stop
    a runaway transfer filling the spool volume, not to duplicate the cap.
    """
    max_pdf_mb = settings.document_max_pdf_size_mb
    if not max_pdf_mb or max_pdf_mb <= 0:
        return None
    return int(max_pdf_mb * 1024 * 1024 * 2)


def preflight_oversize_result(
    doc_task: Any, file_path: str | None, settings: Any
) -> Any:
    """Apply the PDF size cap to the scanned size, before any download.

    ``DocumentTask.size_bytes`` carries the WebDAV ``getcontentlength`` captured
    at scan time, so an over-cap document can be rejected without fetching it.
    Previously the cap could only be applied to bytes already fully resident,
    which meant the guard against pathological files was itself paid for in
    memory: a 531 MB PDF OOMKilled a worker mid-download, before its size was
    ever checked.

    Returns the same ``oversize`` :class:`ProcessingResult` the post-download
    guard produces (so the caller's terminal/dead-letter handling is unchanged),
    or ``None`` when the size is unknown or within the cap -- in which case the
    post-download guard still applies as the backstop.
    """
    # The cap is PDF-specific, but content type is not known before the download.
    # Safe because file discovery is PDF-only: _discover_tagged_files passes
    # mime_type_filter="application/pdf" (vector/scanner.py), so every
    # doc_type="file" task is a PDF. If discovery is ever broadened to other MIME
    # types, gate this on content type rather than silently applying a PDF cap.
    size_bytes = getattr(doc_task, "size_bytes", None)
    if not size_bytes:
        return None
    from nextcloud_mcp_server.document_processors import (  # noqa: PLC0415
        get_registry,
    )

    # Observe the size BEFORE the cap, so the corpus histogram shows the over-cap
    # tail rather than only what we accepted.
    record_document_ingest_size(doc_task.doc_type, size_bytes)
    result = get_registry().oversize_result_for_size(size_bytes, file_path, settings)
    if result is not None:
        record_document_ingest_rejected(doc_task.doc_type, "oversize")
    return result


def ingested_byte_size(source_size: int | None, content: str) -> int:
    """Bytes ingested for one document (card #401).

    Files report the size of the raw WebDAV document — that is what the customer
    ingested — which the caller reads off the document source rather than a
    resident buffer, since a streamed document is never fully in memory. Text
    doc types (note, deck card, news item, mail message) have no binary
    (``source_size is None``), so fall back to the UTF-8 size of the extracted
    text. Selecting on ``source_size is not None`` (not ``doc_type``) keeps this
    correct for any future binary-backed type.
    """
    if source_size is not None:
        return source_size
    return len(content.encode("utf-8"))


def build_point_vector(
    sparse_emb: Any,
    dense_embeddings: list[Any],
    index: int,
    *,
    dense_enabled: bool,
) -> dict[str, Any]:
    """Build the Qdrant named-vector dict for one chunk's point.

    Hybrid docs carry both ``dense`` and ``sparse``. Keyword docs carry only
    ``sparse``: dense embeddings are never generated (``dense_embeddings`` is
    empty), so the point is upserted with a subset of the collection's named
    vectors — valid against the dense+sparse schema. Indexing into an empty
    ``dense_embeddings`` here would be the silent zero-points bug, so the dense
    slot is only read when dense is enabled for this document.
    """
    point_vector: dict[str, Any] = {"sparse": sparse_emb}
    if dense_enabled:
        point_vector["dense"] = dense_embeddings[index]
    return point_vector


def _record_ingest_vector_cost(
    *,
    doc_type: str,
    chunk_count: int,
    source_bytes: int,
    dense_for_doc: bool,
    overhead: float,
) -> None:
    """Emit the observability-only dense-vector cost signals for one document.

    Records the deterministic per-document RAM estimate (hybrid docs only —
    keyword docs embed no dense vector, so the estimate would be 0 anyway, but
    gating on the mode avoids a needless ``get_dimension`` call) and the
    chunk-density histogram (every embedded doc). Card #624.

    Best-effort by contract: this never raises. A metrics failure here must not
    disturb the indexing path, so every failure mode is caught and logged with
    its cause (matching the ``metrics_publisher`` gauge guards) rather than
    propagating.
    """
    try:
        if dense_for_doc:
            dim = get_embedding_service().get_dimension()
            record_estimated_vector_bytes(
                doc_type, estimate_vector_bytes(chunk_count, dim, overhead)
            )
        record_chunk_density(doc_type, chunk_count, source_bytes)
    except Exception as exc:  # noqa: BLE001 — cost metrics must not break indexing
        logger.warning("vector-cost observability hook skipped: %s", exc)


async def record_indexing_usage(
    *,
    enabled: bool,
    provider: str,
    model: str,
    doc_type: str,
    user_id: str,
    index_mode: str,
    chunk_count: int,
    token_count: int,
    total_chars: int,
    page_count: int | None,
    bytes_ingested: int,
    bytes_stored: int,
    pipeline_tier: str | None = None,
) -> None:
    """Record the billable usage events for one indexed document.

    ``index_mode`` (``"hybrid"`` | ``"keyword"``) is stamped on every event's
    metadata so the CP rollup can slice ingestion cost by mode without new metric
    names — a keyword doc (sparse-only, no embedding) and a hybrid doc bill the
    same ``bytes_ingested`` / ``bytes_stored`` metrics, distinguished only by this
    dimension. ``tokens_embedded`` is naturally hybrid-only: keyword docs pass
    ``token_count=0`` and the row is skipped.

    Metered dimensions (Deck #67 / #401), recorded independently:

    - ``tokens_embedded`` — the embedding request's token count, recorded for
      every *hybrid* document (skipped when ``token_count`` is 0, i.e. keyword
      docs). The same metric search records, so the meter bills embedding tokens
      whether they were incurred indexing a document or embedding a query.
    - ``pages_embedded`` — a charge for **parsing** (PDF page extraction / OCR),
      not a normalized content size. ``page_count`` is the real number of pages
      the document processor parsed. Text content (notes, deck cards, news
      items) is never parsed, carries no ``page_count``, and accrues **no**
      ``pages_embedded`` row — only ``tokens_embedded``. There is deliberately
      no chars/tokens-per-page constant: pages map 1:1 to parsed document pages
      (card #282).
    - ``bytes_ingested`` — the raw source size at ingestion time, recorded for
      *every* embedded document. For files this is the raw binary size fetched
      from WebDAV; for text doc types (note, deck card, news item, mail message
      — no binary) it is the UTF-8 size of the extracted text. The caller
      resolves the right source per ``doc_type`` (see the call site).
    - ``bytes_stored`` — the UTF-8 byte size of the chunk texts persisted as
      Qdrant payload excerpts, recorded for *every* embedded document. Reflects
      the indexed footprint and so includes chunk-overlap duplication; it is
      therefore typically larger than ``bytes_ingested`` for text content.

    Best-effort and flag-gated: a metering failure is logged and never breaks
    indexing. ``chunk_count`` is the empty-batch no-op guard — a document that
    produced no chunks embedded nothing, so all events are skipped rather than
    writing zero-value rows. ``pages_embedded`` is additionally skipped when
    ``page_count`` is absent or not strictly positive — gating on the page count
    itself (not the ``doc_type``) keeps this correct if a future non-PDF parsed
    type starts reporting pages, and a malformed non-positive count meters as
    "no pages" rather than emitting a zero/negative billing row. The
    ``bytes_*`` events are likewise skipped on a non-positive count.

    Cross-repo note: the control-plane rollup silently ignores a ``metric`` its
    catalog doesn't know (see migration 007), so ``bytes_ingested`` /
    ``bytes_stored`` only bill once the CP metric catalog + Stripe meter learn
    them too (card #401).

    Privacy note: ``user_id`` stays tenant-local — the CP rollup aggregates
    GROUP BY (day, metric) into ``usage_daily`` (no metadata column), so nothing
    here reaches Stripe; it is retained only to keep Deck #67's future per-user
    attribution derivable from the app DB without a re-migration.
    """
    if not enabled or chunk_count == 0:
        return

    metadata = {
        "provider": provider,
        "model": model,
        "doc_type": doc_type,
        "user_id": user_id,
        # Per-document index mode (card #609): lets the CP rollup slice ingestion
        # cost into hybrid (dense+sparse) vs keyword (sparse-only) without new
        # metric names.
        "index_mode": index_mode,
        "total_chars": total_chars,
        # Which extraction tier produced the parsed pages (Deck #323). Carried so
        # the CP rollup / a future per-tier price can attribute parsing cost to
        # the tier that incurred it (paid OCR vs CPU-cheap fast). None for text
        # doc types, which are never parsed.
        "pipeline_tier": pipeline_tier,
    }
    # Build the document's events in the historical order (tokens -> pages ->
    # pages_ocr -> bytes_*), then write them in ONE transaction. Each event used
    # to be its own acquire() -> INSERT -> commit; on the NullPool + pgbouncer
    # setup every such round-trip pays a full connection setup (~0.6-0.8s on
    # cloudfleet), so ~5 events serialized into seconds on the ingest critical
    # path. Batching collapses them to a single round-trip (Deck #667).
    events: list[UsageEvent] = []

    # tokens_embedded first (intentional ordering): captured before the
    # conditional parsing/byte costs — don't reverse this in a refactor. Skipped
    # for keyword docs (token_count == 0), which never embed, so no zero-value
    # embedding row is written.
    if token_count > 0:
        events.append(
            UsageEvent(metric="tokens_embedded", value=token_count, metadata=metadata)
        )
    # pages_embedded: parsed pages only, and only a strictly positive count. Text
    # content has no page_count; a zero/negative count is skipped rather than
    # writing a row that would misrepresent a no-parse document as billable
    # parsing work.
    if page_count and page_count > 0:
        events.append(
            UsageEvent(metric="pages_embedded", value=page_count, metadata=metadata)
        )
        # OCR pages are metered as a SEPARATE line (Deck #323) so the OCR tier's
        # cost is billable independently of CPU-cheap parsing -- pages_embedded
        # counts all parsed pages, pages_ocr only the OCR tier's. Gated on the
        # tier so it's emitted exactly when the doc hit OCR.
        if pipeline_tier == "ocr":
            events.append(
                UsageEvent(metric="pages_ocr", value=page_count, metadata=metadata)
            )
    # Byte-volume dimensions (card #401), recorded for every embedded document.
    # Appended after the conditional pages block so the tokens-then-pages
    # ordering above is preserved. Each is skipped on a non-positive count to
    # avoid writing a zero-value billing row (the chunk_count guard already
    # filtered truly-empty documents).
    if bytes_ingested > 0:
        events.append(
            UsageEvent(metric="bytes_ingested", value=bytes_ingested, metadata=metadata)
        )
    if bytes_stored > 0:
        events.append(
            UsageEvent(metric="bytes_stored", value=bytes_stored, metadata=metadata)
        )

    try:
        store = await UsageEventStore.shared()
        # enabled=True: the guard above already confirmed the flag, so the store
        # skips a second uncached Settings build (ADR-024). One connection + one
        # commit for the whole document instead of ~5 sequential round-trips.
        await store.record_usage_events(events, enabled=True)
    except Exception:
        # Reached only when shared()/store construction itself raises
        # (record_usage_events swallows its own write failures). Metering is on,
        # so warn rather than hide the "enabled but no billing data" case.
        logger.warning("usage metering hook (indexing embeddings) skipped")


async def processor_task(
    worker_id: int,
    receive_stream: MemoryObjectReceiveStream[DocumentTask],
    shutdown_event: anyio.Event,
    nc_client: NextcloudClient,
    user_id: str,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
):
    """
    Process documents from stream concurrently.

    Each processor task runs in a loop:
    1. Receive document from stream (with timeout)
    2. Fetch content from Nextcloud
    3. Tokenize and chunk text
    4. Generate embeddings (I/O bound - external API)
    5. Upload vectors to Qdrant

    Multiple processors run concurrently for I/O parallelism.

    Args:
        worker_id: Worker identifier for logging
        receive_stream: Stream to receive documents from
        shutdown_event: Event signaling shutdown
        nc_client: Authenticated Nextcloud client
        user_id: User being processed
        task_status: Status object for signaling task readiness
    """
    logger.info("Processor %s started", worker_id)

    # Signal that the task has started and is ready
    task_status.started()

    # Initialised before the loop so the broad except handler below can't hit an
    # unbound name if receive() itself raises a non-TimeoutError/EndOfStream
    # exception on the very first iteration (mirrors multi_user_processor_task).
    doc_task: DocumentTask | None = None

    while not shutdown_event.is_set():
        try:
            # Get document with timeout (allows checking shutdown)
            with anyio.fail_after(1.0):
                doc_task = await receive_stream.receive()

            # Update queue size metric after receiving
            stream_stats = receive_stream.statistics()
            update_vector_sync_queue_size(stream_stats.current_buffer_used)

            # Process document
            await process_document(doc_task, nc_client)

            # Update queue size metric after processing
            stream_stats = receive_stream.statistics()
            update_vector_sync_queue_size(stream_stats.current_buffer_used)

        except TimeoutError:
            # No documents available, update metric to show empty queue
            stream_stats = receive_stream.statistics()
            update_vector_sync_queue_size(stream_stats.current_buffer_used)
            continue

        except anyio.EndOfStream:
            # Scanner finished and closed stream, exit gracefully
            logger.info("Processor %s: Scanner finished, exiting", worker_id)
            break

        except Exception as e:
            if doc_task is not None:
                logger.error(
                    "Processor %s error processing %s_%s: %s",
                    worker_id,
                    doc_task.doc_type,
                    doc_task.doc_id,
                    format_exception_group(e),
                )
            else:
                logger.error(
                    "Processor %s error: %s",
                    worker_id,
                    format_exception_group(e),
                )
            # Continue to next document (no task_done() needed with streams)

    logger.info("Processor %s stopped", worker_id)


async def _reconcile_tag_event(
    doc_task: DocumentTask, nc_client: NextcloudClient
) -> None:
    """Resolve a tag-webhook file task into a concrete index or delete.

    A SystemTag ``MapperEvent`` only tells us a fileid's tags changed — not the
    path, nor which of our index tags is (still) on it. Look up the user's current
    tagged PDFs across BOTH tags (the same discovery the scanner uses, which
    applies hybrid precedence and expands tagged folders into their PDF
    descendants) and reconcile the task in place:

    - fileid present -> index it; fill path/etag/mtime and set ``index_mode`` from
      whichever tag matched (``vector-index`` → hybrid wins over ``keyword-index``).
    - fileid absent  -> it carries neither tag (anymore); flip ``operation`` to
      ``delete`` so any existing points are released for this user.

    A tagged *folder*'s own fileid won't appear in the file-level listing, so it
    resolves to a harmless no-op delete here; the hourly scanner still expands
    tagged folders into their descendants.
    """
    tagged = await _discover_tagged_files(nc_client, get_settings())
    match = next(
        (f for f in tagged if str(f.get("id")) == str(doc_task.doc_id)),
        None,
    )

    if match is None:
        doc_task.operation = "delete"
        logger.info(
            "Tag reconcile: file %s carries no index tag; releasing for %s",
            doc_task.doc_id,
            doc_task.user_id,
        )
        return

    doc_task.index_mode = match.get("_index_mode", payload_keys.INDEX_MODE_HYBRID)
    doc_task.file_path = match["path"]
    # Mirror the scanner's assignment (vector/scanner.py): the SEARCH rows carry
    # getcontentlength as "size". Without this a webhook-triggered index leaves
    # size_bytes None, the pre-flight gate short-circuits, and the unbounded
    # read_file runs -- so tagging a huge PDF for real-time indexing reproduces
    # the OOM the gate exists to prevent. 0/absent means unknown.
    doc_task.size_bytes = match.get("size") or None
    if not doc_task.etag:
        doc_task.etag = match.get("etag")
    last_modified = match.get("last_modified_timestamp")
    if last_modified:
        doc_task.modified_at = int(last_modified)
    logger.info(
        "Tag reconcile: indexing %s (file %s, mode=%s) for %s",
        doc_task.file_path,
        doc_task.doc_id,
        doc_task.index_mode,
        doc_task.user_id,
    )


async def process_document(
    doc_task: DocumentTask,
    nc_client: NextcloudClient,
    *,
    max_retries: int = 3,
    tier: str | None = None,
):
    """
    Process a single document: fetch, tokenize, embed, store in Qdrant.

    Implements retry logic with exponential backoff for transient failures.

    Args:
        doc_task: Document task to process
        nc_client: Authenticated Nextcloud client
        max_retries: In-process indexing attempts before re-raising. The default
            (3) suits the in-process SQLite pool, which has no durable retry. The
            procrastinate worker passes ``1`` so durable retry is owned by the
            queue (and survives worker crashes), avoiding compounding 3×N retries.
        tier: Extraction tier to run for PDFs on the external per-tier path (Deck
            #323) -- the procrastinate worker passes the tier matching its queue.
            ``None`` (the default, used by the in-process/memory pool) runs the
            inline tiered pipeline (``registry.process``: fast -> OCR escalation
            in one call) and never raises ``EscalateError``.

    Retry layering: the embedding provider adds its own transient retry (5
    attempts, 2s→60s backoff — card 309) *inside* each of these attempts. On the
    in-process path (max_retries=3) a sustained outage therefore costs up to
    5×3=15 provider calls (~90s wall-clock) before the document is dropped and
    re-picked on the next scan; the procrastinate path (max_retries=1) caps it at
    one outer attempt (~30s) and defers. Don't stack a third retry layer here.
    """
    # EscalateError and BatchPending are control-flow signals that arise ONLY on
    # the per-tier external path (tier set). Bind them lazily here, and only when a
    # tier is set, so the document stack is never imported at *module load* (the
    # #877 invariant) nor on the delete / text-doc call paths (file processing
    # already imports them via get_registry regardless). When tier is None neither
    # can be raised, so the guards below stay inert. Bound as a tuple so the guards
    # treat both identically: propagate untouched, never record an error/drop.
    control_flow_excs: tuple[type[BaseException], ...] = ()
    if tier is not None:
        from nextcloud_mcp_server.document_processors.escalation import (  # noqa: PLC0415
            BatchPending,
            EscalateError,
        )

        control_flow_excs = (EscalateError, BatchPending)

    start_time = time.time()

    logger.debug(
        "Processing %s_%s for %s (%s)",
        doc_task.doc_type,
        doc_task.doc_id,
        doc_task.user_id,
        doc_task.operation,
    )

    with trace_operation(
        "vector_sync.process_document",
        attributes={
            "vector_sync.operation": "process",
            "vector_sync.user_id": doc_task.user_id,
            "vector_sync.doc_id": doc_task.doc_id,
            "vector_sync.doc_type": doc_task.doc_type,
            "vector_sync.doc_operation": doc_task.operation,
        },
    ):
        try:
            qdrant_client = await get_qdrant_client()

            # Tag-webhook reconcile: a SystemTag MapperEvent enqueues a file task
            # carrying only a fileid (file_path is None — see
            # webhook_parser._parse_tag_event). Resolve the file's current
            # vector-index membership into a concrete index (path/etag filled) or
            # a delete before dispatching below.
            if (
                doc_task.doc_type == "file"
                and doc_task.operation == "index"
                and doc_task.file_path is None
            ):
                await _reconcile_tag_event(doc_task, nc_client)

            # Admin consent gate (Astrolabe): never index a source the admin has
            # disabled for semantic search — this catches near-real-time webhook
            # events that bypass the scanner's discovery gate. Deletes always
            # proceed (removing data honours consent). ``None`` from the reader
            # means no restriction (fail-open / older Astrolabe), so a transient
            # capabilities failure never silently drops indexing.
            if doc_task.operation == "index":
                allowed = await allowed_doc_types(nc_client, doc_task.user_id)
                if not is_doc_type_allowed(doc_task.doc_type, allowed):
                    logger.info(
                        "Skipping index of %s_%s for %s: doc_type disabled by admin",
                        doc_task.doc_type,
                        doc_task.doc_id,
                        doc_task.user_id,
                    )
                    # Alertable counter so a flood of webhook events for a
                    # disabled source is observable (not silently swallowed).
                    record_ingest_dropped("admin_disabled")
                    record_vector_sync_processing(time.time() - start_time, "skipped")
                    return

            # Handle deletion
            if doc_task.operation == "delete":
                # Release this user rather than blind-delete: a file shared across
                # users has one user-agnostic point set referenced by multiple
                # principals, so the points are removed only once the last reader
                # is gone (see vector/sharing_state.release_document_for_user).
                await release_document_for_user(
                    doc_task.doc_id, doc_task.doc_type, doc_task.user_id
                )
                # Drop any dead-letter marker for the file too: release only
                # removes it when the last reader leaves (its filter misses the
                # user-agnostic, principal-less marker), so without this a
                # dead-lettered-then-deleted file would leave an orphan marker
                # accumulating in Qdrant. Only files are ever dead-lettered.
                if doc_task.doc_type == "file":
                    await clear_dead_letter(doc_task.doc_id, doc_task.doc_type)
                logger.info(
                    "Deleted %s_%s for %s",
                    doc_task.doc_type,
                    doc_task.doc_id,
                    doc_task.user_id,
                    extra={
                        "doc_id": doc_task.doc_id,
                        "doc_type": doc_task.doc_type,
                        "status": "success",
                    },
                )

                # Record successful deletion metrics. A delete is not an
                # indexing event, so doc_type is intentionally omitted here to
                # keep it out of astrolabe_documents_indexed_total.
                duration = time.time() - start_time
                record_qdrant_operation("delete", "success")
                record_vector_sync_processing(duration, "success")
                return

            # Handle indexing with retry
            retry_delay = 1.0

            for attempt in range(max_retries):
                try:
                    indexed = await _index_document(
                        doc_task, nc_client, qdrant_client, tier=tier
                    )

                    # A permanent parse failure returns False: it was already
                    # recorded (document_parse_failed_total + the registry's
                    # document_parse_total{error}) and the placeholder marked
                    # "failed". It is not an indexing event and not retryable, so
                    # don't count it as a successful upsert/indexed document.
                    # Identity check, not `if not indexed`: a successful index
                    # (including a dedup hit) returns None, which must NOT be
                    # treated as a parse failure.
                    if indexed is False:
                        return

                    # Record successful processing metrics
                    duration = time.time() - start_time
                    record_qdrant_operation("upsert", "success")
                    record_vector_sync_processing(
                        duration, "success", doc_type=doc_task.doc_type
                    )
                    return  # Success

                except Exception as e:
                    # A control-flow signal (escalation hop, or batch-OCR re-poll
                    # deferral) is not a failure: propagate it untouched so the
                    # procrastinate retry strategy handles it. Never retry it
                    # in-process and never count it as a drop.
                    if isinstance(e, control_flow_excs):
                        raise
                    if attempt < max_retries - 1:
                        logger.warning(
                            "Retry %s/%s for %s_%s: %s",
                            attempt + 1,
                            max_retries,
                            doc_task.doc_type,
                            doc_task.doc_id,
                            format_exception_group(e),
                            extra={
                                "doc_id": doc_task.doc_id,
                                "doc_type": doc_task.doc_type,
                                "attempt": attempt + 1,
                                "max_retries": max_retries,
                                "status": "retry",
                            },
                        )
                        await anyio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        reason = _drop_reason(e)
                        logger.error(
                            "Failed to index %s_%s after %s retries (%s): %s",
                            doc_task.doc_type,
                            doc_task.doc_id,
                            max_retries,
                            reason,
                            format_exception_group(e),
                            extra={
                                "doc_id": doc_task.doc_id,
                                "doc_type": doc_task.doc_type,
                                "attempt": max_retries,
                                "max_retries": max_retries,
                                "status": "error",
                                "drop_reason": reason,
                            },
                        )
                        # Count a failed Qdrant upsert ONLY when Qdrant was the
                        # failing component; an embed/connection failure exhausts
                        # retries before Qdrant is ever called, so attributing it
                        # to mcp_qdrant_operations_total{error} would inflate that
                        # signal. The cause is captured by record_ingest_dropped
                        # instead, and the processing-error metric is recorded
                        # once by the outer handler below (no double-count). The
                        # document is NOT marked failed, so the next scan re-picks
                        # it (re-queue via the scan loop, card 309).
                        if reason == "qdrant":
                            record_qdrant_operation("upsert", "error")
                        record_ingest_dropped(reason)
                        raise

        except Exception as e:
            # A control-flow signal must reach the procrastinate retry strategy
            # un-recorded -- it is neither a processing success nor an error (an
            # escalation hop is counted via record_document_escalation; a batch
            # re-poll deferral is not an event at all).
            if isinstance(e, control_flow_excs):
                raise
            # Single processing-error call site: catches exhausted-retry
            # re-raises, delete failures, and setup errors (get_qdrant_client /
            # get_settings) — each counted exactly once. A failed delete is not
            # an indexing event either, so doc_type is omitted for deletes to
            # keep them out of astrolabe_documents_indexed_total.
            duration = time.time() - start_time
            indexed_doc_type = (
                None if doc_task.operation == "delete" else doc_task.doc_type
            )
            record_vector_sync_processing(duration, "error", doc_type=indexed_doc_type)
            raise


async def _index_document(
    doc_task: DocumentTask,
    nc_client: NextcloudClient,
    qdrant_client,
    *,
    tier: str | None = None,
) -> bool | None:
    """Index a single document, owning any spooled download for its lifetime.

    A streamed document lives in a spool file that must survive parse, chunking,
    embedding and bbox extraction, then be removed however this call ends. The
    exit stack scopes exactly that, so the file cannot outlive the document it
    belongs to and no caller has to remember to unlink it (see
    ``document_processors.source.spool_target``).
    """
    async with AsyncExitStack() as stack:
        return await _index_document_inner(
            doc_task, nc_client, qdrant_client, tier=tier, exit_stack=stack
        )
    return None  # pragma: no cover - AsyncExitStack never swallows here


async def _index_document_inner(
    doc_task: DocumentTask,
    nc_client: NextcloudClient,
    qdrant_client,
    *,
    tier: str | None,
    exit_stack: AsyncExitStack,
) -> bool | None:
    """
    Index a single document (called by process_document with retry).

    ``tier`` selects the external per-tier PDF path (Deck #323): when set and the
    file is a PDF, exactly that tier is parsed and a low-quality result raises
    ``EscalateError`` to hand the document to the next tier's queue. ``None``
    (default) runs the inline tiered pipeline (``registry.process``).

    Returns ``False`` when a permanent parse failure means nothing was indexed
    (the caller must then skip the success metrics); ``None`` otherwise.

    Args:
        doc_task: Document task to index
        nc_client: Authenticated Nextcloud client
        qdrant_client: Qdrant client instance
    """
    settings = get_settings()
    # Set by the pre-flight size gate (files only) when a document is rejected
    # from its scanned size; substituted for the parse result further down so the
    # existing terminal/dead-letter handling is reached unchanged.
    preflight_failure = None
    # The document handle for files; None for text doc types (note, deck card,
    # news item, mail message), which carry no binary.
    source: DocumentSource | None = None

    # Fetch document content
    with trace_operation(
        "vector_sync.fetch_content",
        attributes={
            "vector_sync.doc_type": doc_task.doc_type,
            "vector_sync.doc_id": doc_task.doc_id,
        },
    ):
        if doc_task.doc_type == "note":
            document = await nc_client.notes.get_note(int(doc_task.doc_id))
            content = f"{document['title']}\n\n{document['content']}"
            title = document["title"]
            etag = document.get("etag", "")
            file_metadata = {}  # No file-specific metadata for notes
            file_path = None  # Notes don't have file paths
            content_bytes = None  # Notes don't have binary content
            content_type = None
        elif doc_task.doc_type == "news_item":
            item = await nc_client.news.get_item(int(doc_task.doc_id))
            # Convert HTML body to Markdown for better embedding
            body_markdown = html_to_markdown(item.get("body", ""))
            # Build content: title + URL + body
            item_title = item.get("title", "")
            item_url = item.get("url", "")
            feed_title = item.get("feedTitle", "")

            # Structure content for embedding
            content_parts = [item_title]
            if feed_title:
                content_parts.append(f"Source: {feed_title}")
            if item_url:
                content_parts.append(f"URL: {item_url}")
            content_parts.append("")  # Blank line
            content_parts.append(body_markdown)
            content = "\n".join(content_parts)

            title = item_title
            etag = item.get("guidHash", "")
            # Store news-specific metadata for later use in payload
            file_metadata = {
                "feed_id": item.get("feedId"),
                "feed_title": feed_title,
                "author": item.get("author"),
                "pub_date": item.get("pubDate"),
                "starred": item.get("starred", False),
                "unread": item.get("unread", True),
                "url": item_url,
                "guid_hash": item.get("guidHash"),
                "enclosure_link": item.get("enclosureLink"),
                "enclosure_mime": item.get("enclosureMime"),
            }
            file_path = None
            content_bytes = None
            content_type = None
        elif doc_task.doc_type == "mail_message":
            # Fetch the full message via the Mail OCS API. The Mail app handles
            # IMAP server-side; we only ever speak HTTP. build_mail_content is
            # shared with search/context.py so index- and query-time text match.
            # Guard the cast before the network call (consistent with the same
            # doc_type in search/context.py) so a malformed queue record produces
            # a specific error rather than a bare ValueError.
            if not is_valid_nextcloud_doc_id(doc_task.doc_id):
                raise ValueError(f"Invalid mail_message doc_id: {doc_task.doc_id!r}")
            message = await nc_client.mail.get_message(int(doc_task.doc_id))
            # An empty payload (OCS data=null with a <400 meta) would otherwise
            # index a useless near-empty placeholder; fail loudly so the task
            # dead-letters instead of corrupting the index.
            if not message:
                raise ValueError(
                    f"mail_message {doc_task.doc_id!r} returned an empty payload"
                )
            content = build_mail_content(message)

            subject = message.get("subject") or ""
            title = subject
            # Email is immutable; key change-detection on the message id so a
            # re-index is a no-op unless the id changes.
            etag = str(message.get("id") or doc_task.doc_id)
            file_metadata = {
                "subject": subject,
                "from": format_mail_addresses(message.get("from")),
                "to": format_mail_addresses(message.get("to")),
                "cc": format_mail_addresses(message.get("cc")),
                "bcc": format_mail_addresses(message.get("bcc")),
                "date_int": message.get("dateInt"),
                "has_attachments": bool(message.get("attachments")),
                "account_id": (doc_task.metadata or {}).get("account_id"),
                "mailbox_id": (doc_task.metadata or {}).get("mailbox_id"),
            }
            file_path = None
            content_bytes = None
            content_type = None
        elif doc_task.doc_type == "deck_card":
            # Fetch card from Deck API
            # Use metadata from scanner if available (O(1) lookup)
            # Otherwise fall back to iteration (legacy data)
            card = None
            board = None
            stack = None

            if (
                doc_task.metadata
                and "board_id" in doc_task.metadata
                and "stack_id" in doc_task.metadata
            ):
                # Fast path: Direct lookup with known board_id/stack_id
                board_id = doc_task.metadata["board_id"]
                stack_id = doc_task.metadata["stack_id"]
                try:
                    card = await nc_client.deck.get_card(
                        board_id=int(board_id),
                        stack_id=int(stack_id),
                        card_id=int(doc_task.doc_id),
                    )
                    # Fetch board and stack info for metadata
                    boards = await nc_client.deck.get_boards()
                    for b in boards:
                        if b.id == int(board_id):
                            board = b
                            stacks = await nc_client.deck.get_stacks(b.id)
                            for s in stacks:
                                if s.id == int(stack_id):
                                    stack = s
                                    break
                            break
                except Exception as e:
                    logger.warning(
                        "Failed to fetch card with metadata (board_id=%s, stack_id=%s, card_id=%s): %s, falling back to iteration",
                        board_id,
                        stack_id,
                        doc_task.doc_id,
                        e,
                    )

            # Fallback: Iterate through all boards/stacks (for legacy data or if fast path failed)
            if card is None:
                boards = await nc_client.deck.get_boards()
                card_found = False

                for b in boards:
                    if card_found:
                        break
                    # Skip deleted boards (soft delete: deletedAt > 0)
                    if b.deletedAt > 0:
                        continue
                    stacks = await nc_client.deck.get_stacks(b.id)
                    for s in stacks:
                        if card_found:
                            break
                        if s.cards:
                            # get_stacks() always yields full DeckCard objects;
                            # the DeckCardSummary projection only happens in the
                            # tool layer, never on freshly-fetched stacks.
                            for c in cast(list[DeckCard], s.cards):
                                if c.id == int(doc_task.doc_id):
                                    card = c
                                    board = b
                                    stack = s
                                    card_found = True
                                    break

                if not card_found:
                    raise ValueError(
                        f"Deck card {doc_task.doc_id} not found in any board/stack"
                    )

            # Type narrowing: card, board, stack are all set if we reach here
            assert card is not None
            assert board is not None
            assert stack is not None

            # Build content from card title and description
            content_parts = [card.title]
            if card.description:
                content_parts.append(card.description)
            content = "\n\n".join(content_parts)
            title = card.title

            # Store deck-specific metadata
            file_metadata = {
                "board_id": board.id,
                "board_title": board.title,
                "stack_id": stack.id,
                "stack_title": stack.title,
                "card_type": card.type,
                "duedate": (card.duedate.isoformat() if card.duedate else None),
                "archived": card.archived,
                "owner": (
                    card.owner.uid if hasattr(card.owner, "uid") else str(card.owner)
                ),
            }
            etag = card.etag or ""
            file_path = None
            content_bytes = None
            content_type = None
        elif doc_task.doc_type == "file":
            # For files, doc_id is now the numeric file ID, file_path comes from DocumentTask
            if not doc_task.file_path:
                raise ValueError(
                    f"File path required for file indexing but not provided (file_id={doc_task.doc_id})"
                )
            file_path = doc_task.file_path

            # Cross-worker dedup race-guard: two users' tasks for the same shared
            # file can be enqueued before either finishes. If another worker has
            # already indexed this exact content (fileid + etag + embedding model)
            # in the tenant, claim it for this user (observed-access ACL) and skip
            # the expensive fetch/parse/embed entirely.
            if doc_task.etag and await claim_existing_index(
                doc_task.doc_id,
                "file",
                doc_task.etag,
                doc_task.user_id,
                index_mode=doc_task.index_mode,
                current_path=doc_task.file_path,
            ):
                await delete_placeholder_point(
                    doc_id=doc_task.doc_id,
                    doc_type="file",
                    user_id=doc_task.user_id,
                )
                # No embedding ran, so no usage is recorded here — stated
                # explicitly so a "fewer tokens_embedded rows than expected"
                # audit lands on the dedup path rather than reconstructing it
                # from Qdrant claim logs.
                logger.info(
                    "Dedup hit for file %s (etag=%s); claimed for user %s "
                    "without reprocessing (no embedding/usage recorded)",
                    doc_task.doc_id,
                    doc_task.etag,
                    doc_task.user_id,
                )
                return

            # Deck #516: the file bytes are needed only to SUBMIT an OCR job — a
            # poll uses the gateway job_id and indexing uses the OCR text the gateway
            # returns, not the original bytes. When a batch OCR job is already in
            # flight for this content, poll it FIRST and, while it is still pending,
            # defer WITHOUT the WebDAV re-download that otherwise ran on EVERY retry
            # (~half the OCR worker's single-slot wall-time; the GPU starved while the
            # worker re-fetched files). Only a TERMINAL poll falls through to the
            # download + index path below (which re-polls and needs the real bytes for
            # the post-parse quality gate). Gated on ``tier == "ocr"``: only the OCR
            # tier ever writes rows to BatchOcrJobStore, so fast/structured tiers skip
            # the store lookup entirely, and it's the per-tier worker path where
            # ``BatchPending`` is a handled control-flow signal.
            if (
                tier == "ocr"
                and settings.document_ocr_mode == "batch"
                and doc_task.etag
            ):
                from nextcloud_mcp_server.document_processors.escalation import (  # noqa: PLC0415
                    BatchPending,
                )
                from nextcloud_mcp_server.document_processors.ocr import (  # noqa: PLC0415
                    poll_pending_batch_ocr,
                )

                retry_in = await poll_pending_batch_ocr(
                    user_id=doc_task.user_id,
                    doc_id=doc_task.doc_id,
                    etag=doc_task.etag,
                    settings=settings,
                )
                if retry_in is not None:
                    raise BatchPending(retry_in=retry_in)

            # Pre-flight size gate: reject an over-cap document from the size the
            # scanner already captured, so the download is never paid for. Falls
            # through to the post-download guard when the size is unknown.
            preflight_failure = preflight_oversize_result(doc_task, file_path, settings)
            if preflight_failure is not None:
                content_bytes, content_type = b"", PDF_MIME_TYPE
            elif settings.document_stream_download_enabled:
                # Stream to a spool file: resident memory stays at one chunk
                # instead of the whole document. The stack owns the file until
                # this document is fully indexed.
                source = await exit_stack.enter_async_context(
                    spooled_document(
                        nc_client,
                        file_path,
                        spool_dir=settings.document_spool_dir,
                        max_bytes=_download_ceiling(settings),
                    )
                )
                content_type = source.content_type
                content_bytes = None
            else:
                # Buffered fallback (DOCUMENT_STREAM_DOWNLOAD_ENABLED=false).
                content_bytes, content_type = await nc_client.webdav.read_file(
                    file_path
                )
                source = MemoryDocumentSource(
                    content_bytes,
                    content_type,
                    file_path,
                    spool_dir=settings.document_spool_dir,
                )
                # Same lifetime guarantee as the streamed branch. path() lazily
                # materialises the buffer to a temp file -- both PDF engines and
                # the bbox step call it -- so without this the kill-switch path
                # leaks one file per document, which is the very failure mode the
                # streaming side's exit-stack scoping exists to prevent.
                exit_stack.callback(source.cleanup)
        else:
            raise ValueError(f"Unsupported doc_type: {doc_task.doc_type}")

    # Process file content (text extraction)
    if doc_task.doc_type == "file":
        # Type narrowing: content_type/file_path are set for files. content_bytes
        # is None on the streamed path -- the document lives in ``source``.
        assert content_type is not None
        assert file_path is not None

        with trace_operation(
            "vector_sync.document_process",
            attributes={
                "vector_sync.content_type": content_type,
                # The scanned size when known, so a document rejected before its
                # download still reports its real size rather than 0.
                "vector_sync.file_size": (
                    source.size if source is not None else (doc_task.size_bytes or 0)
                ),
            },
        ):
            # The registry runs the tiered PDF pipeline and records
            # classification metrics. Imported lazily so module import doesn't
            # pull in the document stack (document_processors -> _isolation,
            # Unix-only ``resource``; see #877).
            from nextcloud_mcp_server.document_processors import (  # noqa: PLC0415
                get_registry,
            )
            from nextcloud_mcp_server.document_processors.escalation import (  # noqa: PLC0415
                TIER_LADDER,
                BatchPending,
                EscalateError,
                escalation_tiers_signature,
            )

            registry = get_registry()

            try:
                # External per-tier path (Deck #323): run only this worker's tier
                # for PDFs and let a low-quality parse raise EscalateError (a
                # queue-hop to the next tier). Everything else -- non-PDF files,
                # and the in-process/memory pool (tier is None) -- runs the inline
                # tiered pipeline (fast -> OCR escalation in one call).
                if preflight_failure is not None:
                    # Rejected from its scanned size before the download, so
                    # there is nothing to parse. Substituting the guard's result
                    # here (rather than returning early) keeps oversize handling
                    # on the single terminal/dead-letter path below, however the
                    # size became known.
                    result = preflight_failure
                elif tier is not None and _is_pdf(content_type):
                    # Narrowing: the download branch above always sets ``source``
                    # when it did not short-circuit on the pre-flight gate.
                    assert source is not None
                    # Per-document identity, forwarded to every tier's processor.
                    # Only the OCR tier reads it (batch mode keys its job-tracking
                    # table on it, Deck #332); fast/structured ignore it, so it's
                    # safe to pass on all tiers.
                    doc_identity_options = {
                        "user_id": doc_task.user_id,
                        "doc_id": doc_task.doc_id,
                        "doc_type": doc_task.doc_type,
                        "etag": doc_task.etag or "",
                    }
                    result = await _parse_pdf_tier(
                        registry,
                        source,
                        tier,
                        settings,
                        options=doc_identity_options,
                    )
                else:
                    assert source is not None
                    result = await registry.process_source(source)

                # A permanent parse failure (e.g. an isolated-worker OOM/timeout
                # on a pathological PDF) returns success=False rather than
                # raising -- there is nothing to index and retrying would just
                # fail again.
                if not result.success:
                    reason = result.metadata.get("parse_failed_reason", "error")
                    # The tier that produced this failed result: the worker's own
                    # tier on the per-tier path, else the deepest tier the inline
                    # pipeline reached (recorded as ``pipeline_tier``).
                    failing_tier = tier or result.metadata.get(
                        "pipeline_tier", TIER_LADDER[0]
                    )
                    # The next tier that can still run above the failing one
                    # (``None`` == terminal). An oversize PDF is rejected by the
                    # pre-parse size guard before any tier runs (no pipeline_tier
                    # stamped on the inline path) and no tier can ever parse it, so
                    # it is terminal regardless of failing_tier.
                    next_avail = (
                        None
                        if reason == "oversize"
                        else registry.next_available_tier(failing_tier, settings)
                    )
                    # #399: a hard parse failure (an isolated-worker timeout/OOM on
                    # a pathological PDF) is not necessarily terminal. On the
                    # per-tier path, if a higher tier can still run, ESCALATE the
                    # failure to it rather than dropping the doc -- e.g. a
                    # structured-tier pymupdf timeout on a garbled/huge plan hops to
                    # OCR, where surya rasterizes + reads the rendered glyphs.
                    # Without this the doc was marked "failed" and the scanner
                    # re-queued it into the same failing tier forever (the retry
                    # loop seen on 406-105-style plans). The inline pipeline (tier
                    # is None) already ran every tier in one call, so it never hops
                    # here -- its failures fall through to the terminal handling.
                    if tier is not None and next_avail is not None:
                        # Mirror the quality-gate hop in _parse_pdf_tier: record the
                        # escalation + raise so the retry strategy requeues onto the
                        # next tier's queue. Deliberately NOT counted as a parse
                        # failure (no record_document_parse_failed / failed mark) --
                        # that would both inflate the hard-parse-failure panel and
                        # lose a document OCR can still read.
                        record_document_escalation(failing_tier, next_avail, reason)
                        logger.info(
                            "Parse failure for %s at tier=%s (reason=%s); "
                            "escalating to %s",
                            file_path,
                            failing_tier,
                            reason,
                            next_avail,
                        )
                        raise EscalateError(
                            from_tier=failing_tier,
                            to_tier=next_avail,
                            reason=reason,
                        )
                    # Terminal: oversize, the inline pipeline exhausted every tier,
                    # or the deepest per-tier rung failed. Count the failure and
                    # dead-letter / mark it below.
                    record_document_parse_failed(reason)
                    terminal = next_avail is None
                    if terminal and doc_task.etag:
                        # No higher tier can run (e.g. structured timed out with
                        # OCR off), so retrying just re-burns the same failing
                        # parse. Dead-letter the document tenant-wide
                        # (content-addressed, user-agnostic) so EVERY user's scan
                        # stops re-queuing it until its content (etag) or the
                        # escalation-tier set (e.g. OCR enabled -> new tiers_sig)
                        # changes. This fixes the multi-user placeholder
                        # ping-pong the per-user "failed" mark could not: a file
                        # shared by N users has ONE user-agnostic placeholder
                        # whose user_id is overwritten by the last scanner, so
                        # every other user re-queued it forever. Requires an etag
                        # to content-address the marker; without one (rare) we
                        # fall back to the legacy per-user mark below.
                        await mark_dead_letter(
                            doc_task.doc_id,
                            doc_task.doc_type,
                            doc_task.etag,
                            escalation_tiers_signature(settings),
                            reason,
                            file_path=file_path,
                        )
                        record_document_dead_lettered(reason)
                        logger.warning(
                            "Permanent parse failure for %s (reason=%s); "
                            "dead-lettered (terminal tier=%s, no escalation) and "
                            "skipping index",
                            file_path,
                            reason,
                            failing_tier,
                        )
                        # Drop the volatile in-flight placeholder; the durable
                        # marker is now the document's terminal-state record.
                        try:
                            await delete_placeholder_point(
                                doc_id=doc_task.doc_id,
                                doc_type=doc_task.doc_type,
                                user_id=doc_task.user_id,
                            )
                        except Exception:
                            # A real Qdrant I/O failure (not control-flow): warn so
                            # it's observable. Non-fatal -- the durable dead-letter
                            # marker is already written, so the leftover volatile
                            # placeholder is merely redundant.
                            logger.warning(
                                "Could not delete placeholder for dead-lettered %s",
                                doc_task.doc_id,
                            )
                    else:
                        # Either a higher tier exists (parse failures don't
                        # escalate to it today) or there's no etag to
                        # content-address a dead-letter marker. Keep the legacy
                        # per-user "failed" placeholder mark.
                        logger.warning(
                            "Permanent parse failure for %s (reason=%s); marking "
                            "failed and skipping index",
                            file_path,
                            reason,
                        )
                        try:
                            await update_placeholder_status(
                                doc_id=doc_task.doc_id,
                                doc_type=doc_task.doc_type,
                                user_id=doc_task.user_id,
                                status="failed",
                            )
                        except Exception:
                            # Best-effort: a transient Qdrant error here only
                            # means the placeholder isn't marked, so the scanner
                            # retries the (still un-indexable) file later.
                            logger.debug(
                                "Could not mark placeholder failed for %s",
                                doc_task.doc_id,
                            )
                    return False

                content = result.text
                file_metadata = result.metadata
                # Favour the Nextcloud filename over any embedded document title
                # (e.g. a PDF's /Title), which often disagrees with how the user
                # named the file and is confusing in the UI.
                title = file_title_from_path(file_path)
                # etag comes from the scanner's tag REPORT (threaded via the
                # DocumentTask); read_file itself returns no etag. It is the
                # tenant-wide content-dedup key, so it must be persisted.
                etag = doc_task.etag or ""

                # Diagnostic: Log page boundary information if available
                if "page_boundaries" in file_metadata:
                    page_boundaries = file_metadata["page_boundaries"]
                    logger.debug(
                        "Page boundaries for %s: %s pages, text length: %s",
                        file_path,
                        len(page_boundaries),
                        len(content),
                    )
                    # Verify last boundary matches text length
                    if page_boundaries:
                        last_boundary = page_boundaries[-1]
                        if last_boundary["end_offset"] != len(content):
                            logger.warning(
                                "Text length mismatch: content=%s, last_boundary_end=%s",
                                len(content),
                                last_boundary["end_offset"],
                            )
                else:
                    logger.debug("No page_boundaries in metadata for %s", file_path)
            except (EscalateError, BatchPending):
                # Control-flow signals (per-tier path): re-raise untouched.
                # EscalateError hops the job to the next tier; BatchPending defers
                # a re-poll on the same tier (batch OCR still in flight, Deck
                # #332). Neither is a "failed to process" error -- don't log them
                # as one.
                raise
            except Exception as e:
                logger.error("Failed to process file %s: %s", file_path, e)
                raise

    # Tokenize and chunk (using configured chunk size and overlap). Paginated
    # files (PDFs with page_boundaries) use the page-aware chunker when enabled,
    # which assigns page numbers inline; everything else uses the char-based
    # chunker followed by post-hoc page assignment.
    page_boundaries = file_metadata.get("page_boundaries")
    use_page_aware = should_use_page_aware(
        page_aware_enabled=settings.document_chunk_page_aware,
        doc_type=doc_task.doc_type,
        page_boundaries=page_boundaries,
    )
    with trace_operation(
        "vector_sync.chunk_text",
        attributes={
            "vector_sync.input_chars": len(content),
            "vector_sync.chunk_size": settings.document_chunk_size,
            "vector_sync.overlap": settings.document_chunk_overlap,
            "vector_sync.page_aware": use_page_aware,
        },
    ) as chunk_span:
        if use_page_aware:
            page_boundaries_list = cast(list[dict[str, Any]], page_boundaries)
            chunks = await PageAwareChunker(
                chunk_size=settings.document_chunk_size,
                overlap=settings.document_chunk_overlap,
                pack_pages=settings.document_chunk_page_pack,
            ).chunk_text(content, page_boundaries_list)
        else:
            chunks = await DocumentChunker(
                chunk_size=settings.document_chunk_size,
                overlap=settings.document_chunk_overlap,
            ).chunk_text(content)
        record_document_chunks(doc_task.doc_type, len(chunks))
        if chunk_span is not None:
            chunk_span.set_attribute(_ATTR_CHUNK_COUNT, len(chunks))

    # Assign page numbers for the char-based path (page-aware already sets them).
    # Truthy guard (not "is not None"): an empty boundary list has nothing to
    # assign, so skip the span and the "NO page numbers assigned" warning.
    if not use_page_aware and doc_task.doc_type == "file" and page_boundaries:
        # Type narrowing: page_boundaries is guaranteed to be list[dict] here
        page_boundaries_list = cast(list[dict[str, Any]], page_boundaries)
        with trace_operation(
            "vector_sync.assign_page_numbers",
            attributes={
                _ATTR_CHUNK_COUNT: len(chunks),
                "vector_sync.page_count": len(page_boundaries_list),
            },
        ):
            assign_page_numbers(chunks, page_boundaries_list)

            # Diagnostic: Verify page number assignment
            assigned_count = sum(1 for c in chunks if c.page_number is not None)
            logger.debug(
                "Assigned page numbers to %s/%s chunks for %s",
                assigned_count,
                len(chunks),
                file_path,
            )

            # Warning if NO page numbers were assigned
            if assigned_count == 0:
                logger.warning(
                    "NO page numbers assigned! Text length: %s, Chunks: %s, Chunk offset range: [%s:%s], Page boundaries: %s pages, First boundary: %s",
                    len(content),
                    len(chunks),
                    chunks[0].start_offset,
                    chunks[-1].end_offset,
                    len(page_boundaries_list),
                    page_boundaries_list[0] if page_boundaries_list else "None",
                )

    # Extract chunk texts for embedding
    chunk_texts = [chunk.text for chunk in chunks]

    # Per-document index mode (per-document keyword vs hybrid). Keyword docs
    # (``keyword-index`` tag) skip dense embeddings entirely and upsert
    # sparse-only points into the shared collection; hybrid docs (default) carry
    # both dense and sparse. This replaces the removed global ``dense_enabled``.
    dense_for_doc = doc_task.index_mode != payload_keys.INDEX_MODE_KEYWORD

    # Initialize results containers
    dense_embeddings: list = []
    sparse_embeddings: list = []
    # Embedding-token count from the dense pass (0 for keyword docs, which never
    # embed). Captured as a nonlocal so the post-task-group metering can record
    # ``tokens_embedded`` for hybrid docs while byte/page metering covers both.
    dense_embed_tokens: int = 0
    # chunk_index -> list[(x0, y0, x1, y1)] of normalized rectangles
    # in [0, 1] relative to page width/height. The page is taken from
    # `chunk.page_number` (offset-based) and stored as `page_number`
    # in the Qdrant payload, so we don't carry an `actual_page_num` here.
    chunk_bboxes: dict[int, list[tuple[float, float, float, float]]] = {}
    # Where the bboxes came from — "ocr" (gateway-provided per-block geometry) or
    # "pymupdf" (local text-search). Stamped on each chunk payload that has a bbox.
    bbox_source: str | None = None

    # Determine if we need PDF highlighting
    is_pdf = doc_task.doc_type == "file" and content_type == PDF_MIME_TYPE

    # Define async tasks for parallel execution
    async def generate_dense_embeddings():
        """Generate dense embeddings (I/O bound - external API call)."""
        nonlocal dense_embeddings, dense_embed_tokens
        provider = settings.get_embedding_provider_family()
        total_chars = sum(len(t) for t in chunk_texts)
        with trace_operation(
            "vector_sync.embed_dense",
            attributes={
                _ATTR_CHUNK_COUNT: len(chunk_texts),
                "vector_sync.total_chars": total_chars,
                "embedding.kind": "dense",
                "embedding.provider": provider,
                "embedding.model": settings.get_embedding_model_name(),
            },
        ):
            embedding_service = get_embedding_service()
            embed_start = time.time()
            try:
                (
                    dense_embeddings,
                    embed_tokens,
                ) = await embedding_service.embed_batch_with_usage(chunk_texts)
            except Exception:
                record_embedding(
                    "dense", provider, time.time() - embed_start, status="error"
                )
                raise
            record_embedding(
                "dense",
                provider,
                time.time() - embed_start,
                chunks=len(chunk_texts),
                chars=total_chars,
            )
            # Export token consumption to Prometheus (always-on, independent of
            # the billing flag) so Grafana sees indexing token cost.
            record_embedding_tokens(provider, "index", embed_tokens)
            # Hand the token count to the post-task-group usage metering (byte +
            # page dimensions are recorded there for BOTH modes; embedding tokens
            # are hybrid-only, so keyword docs keep the initialised 0).
            dense_embed_tokens = embed_tokens

    async def generate_sparse_embeddings():
        """Generate sparse embeddings (BM25 for keyword matching)."""
        nonlocal sparse_embeddings
        total_chars = sum(len(t) for t in chunk_texts)
        with trace_operation(
            "vector_sync.embed_sparse",
            attributes={
                _ATTR_CHUNK_COUNT: len(chunk_texts),
                "vector_sync.total_chars": total_chars,
                "embedding.kind": "sparse",
                "embedding.provider": "bm25",
            },
        ):
            bm25_service = await get_bm25_service()
            embed_start = time.time()
            try:
                sparse_embeddings = await bm25_service.encode_batch(chunk_texts)
            except Exception:
                record_embedding(
                    "sparse", "bm25", time.time() - embed_start, status="error"
                )
                raise
            record_embedding(
                "sparse",
                "bm25",
                time.time() - embed_start,
                chunks=len(chunk_texts),
                chars=total_chars,
            )

    async def generate_highlights():
        """Compute chunk bounding boxes for PDF chunks (CPU-bound, no rendering).

        Prefers OCR-provided geometry: when the OCR tier returned per-block bboxes
        (surya via the gateway, normalized [0,1] — ``OCR_BLOCK_SPANS_KEY`` in
        metadata), a chunk's bbox is the set of blocks whose char span it overlaps,
        and ``bbox_source`` is ``"ocr"``. This is the ONLY viable source for an
        OCR'd (scanned) doc — its PDF has no text layer for pymupdf to search. When
        no OCR geometry is present (fast/structured tiers, Mistral OCR), fall back
        to the pymupdf text-search path (``bbox_source="pymupdf"``)."""
        nonlocal chunk_bboxes, bbox_source
        if not is_pdf:
            return

        # The document is in ``source`` (streamed path) or ``content_bytes``
        # (buffered fallback); both are set for PDF files.
        assert source is not None

        # Lazy import (mirrors _parse_pdf_tier): keep the document stack off the
        # module load path; this runs only for PDFs on the indexing path.
        from nextcloud_mcp_server.document_processors.ocr import (  # noqa: PLC0415
            OCR_BLOCK_SPANS_KEY,
        )

        # Envelope span so the WHOLE highlight step is visible in traces — both the
        # OCR-attribution branch (previously untraced) and the pymupdf branch — and
        # the off-thread bbox CPU no longer shows up as an unattributed gap.
        with trace_operation(
            "vector_sync.generate_highlights",
            attributes={
                _ATTR_CHUNK_COUNT: len(chunks),
                "vector_sync.is_pdf": is_pdf,
                "vector_sync.pdf_size": source.size,
            },
        ) as highlights_span:

            def _stamp_bbox_source() -> None:
                """Record which branch produced the bboxes on the envelope span.

                Called on every exit path so a trace query by bbox_source never
                finds a highlight span with the attribute missing.
                """
                if highlights_span is not None:
                    highlights_span.set_attribute(
                        "vector_sync.bbox_source", bbox_source or "none"
                    )

            ocr_block_spans = file_metadata.get(OCR_BLOCK_SPANS_KEY)
            if ocr_block_spans:
                spans = cast(list[dict[str, Any]], ocr_block_spans)
                with trace_operation(
                    "vector_sync.ocr_chunk_bboxes",
                    attributes={
                        _ATTR_CHUNK_COUNT: len(chunks),
                        "vector_sync.ocr_block_count": len(spans),
                    },
                ):
                    attributed = _ocr_chunk_bboxes(chunks, spans)
                    chunk_bboxes.update(attributed)
                    # Only stamp "ocr" when something was actually attributed — a
                    # non-empty spans list that overlaps no chunk leaves the source
                    # unset (nothing is stored either way; the payload gates on
                    # `i in chunk_bboxes`).
                    if attributed:
                        bbox_source = "ocr"
                        logger.info(
                            "Attributed OCR bboxes for %s/%s chunks (%s blocks)",
                            len(attributed),
                            len(chunks),
                            len(spans),
                        )
                    else:
                        # OCR returned geometry but none overlapped a chunk —
                        # unexpected (offset accounting / empty chunks); warn so it
                        # is visible.
                        logger.warning(
                            "OCR returned %s blocks but none overlapped any of %s "
                            "chunks; no pre-computed bboxes stored",
                            len(spans),
                            len(chunks),
                        )
                # One path for the WHOLE document: when the OCR tier ran, surya OCRs
                # every rendered page, so its blocks cover the whole doc — pymupdf has
                # no text layer to add (a scanned doc) and we do NOT fall through to
                # it, even when attribution found nothing. Caveat: a mixed native+OCR
                # PDF gets OCR geometry only; native-page chunks won't also get
                # pymupdf highlights. That's acceptable — escalation to OCR is a
                # whole-document decision, so a mixed doc is rare, and unmatched
                # blocks are logged in _pages_to_text for diagnosis.
                _stamp_bbox_source()
                return

            with trace_operation(
                "vector_sync.compute_chunk_bboxes",
                attributes={
                    _ATTR_CHUNK_COUNT: len(chunks),
                    "vector_sync.pdf_size": source.size,
                },
            ):
                chunk_data: list[tuple[int, int, int, int | None, str]] = [
                    (
                        i,
                        chunk.start_offset,
                        chunk.end_offset,
                        chunk.page_number,
                        chunk.text,
                    )
                    for i, chunk in enumerate(chunks)
                    if chunk.page_number is not None
                ]

                page_boundaries = file_metadata.get("page_boundaries")
                if not page_boundaries:
                    logger.warning(
                        "No page boundaries available, skipping bbox computation"
                    )
                    _stamp_bbox_source()
                    return

                page_boundaries_list = cast(list[dict[str, Any]], page_boundaries)

                logger.info("Computing chunk bboxes for %s PDF chunks", len(chunk_data))

                # pymupdf is not thread-safe; run the bbox batch under the shared
                # MuPDF lock so concurrent ingest jobs serialize their native work.
                def _compute_bboxes():
                    from nextcloud_mcp_server.document_processors._native_locks import (  # noqa: PLC0415
                        pymupdf_serialized,
                    )

                    with pymupdf_serialized():
                        return PDFHighlighter.compute_chunk_bboxes_batch(
                            pdf_bytes=None,
                            pdf_path=source.path(),
                            chunks=chunk_data,
                            page_boundaries=page_boundaries_list,
                            full_text=content,
                        )

                batch_results = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                    _compute_bboxes
                )

                for chunk_index, (bboxes, _) in batch_results.items():
                    chunk_bboxes[chunk_index] = bboxes
                if chunk_bboxes:
                    bbox_source = "pymupdf"

                logger.info(
                    "Computed bboxes for %s/%s chunks", len(chunk_bboxes), len(chunks)
                )
            _stamp_bbox_source()

    # Run all embedding/highlighting operations in parallel
    # - Dense embeddings: I/O bound (API call)
    # - Sparse embeddings: CPU bound (local BM25)
    # - Highlighting: CPU bound (PyMuPDF rendering, runs in thread pool)
    with trace_operation(
        "vector_sync.parallel_processing",
        attributes={
            "vector_sync.is_pdf": is_pdf,
            _ATTR_CHUNK_COUNT: len(chunks),
        },
    ):
        async with anyio.create_task_group() as tg:
            # Keyword-only documents (``keyword-index`` tag → index_mode
            # "keyword") skip dense embeddings entirely: no embedding endpoint is
            # contacted and the point is upserted sparse-only. ``dense_embeddings``
            # stays [] in that case. Hybrid documents (default) always embed — a
            # failed/unavailable embedding endpoint raises out of
            # generate_dense_embeddings into process_document's retry/dead-letter
            # path rather than silently degrading to sparse-only.
            if dense_for_doc:
                tg.start_soon(generate_dense_embeddings)
            tg.start_soon(generate_sparse_embeddings)
            tg.start_soon(generate_highlights)

    # Usage metering (Deck #67), recorded once per document AFTER the embedding/
    # sparse task group so it covers BOTH modes (byte + page dimensions for
    # keyword and hybrid alike; embedding tokens are hybrid-only via
    # dense_embed_tokens, which stays 0 for keyword docs). Best-effort and
    # flag-gated; placed after processing succeeds so it can never affect the
    # indexing path. ``page_count`` is set by the document processors for PDFs
    # and absent for text types. Narrow defensively: file_metadata values are
    # loosely typed, so a malformed page_count meters as "no pages"; bool is an
    # int subclass, so exclude it explicitly.
    _meter_total_chars = sum(len(t) for t in chunk_texts)
    _meter_raw_page_count = file_metadata.get("page_count")
    _meter_pipeline_tier = file_metadata.get("pipeline_tier")
    await record_indexing_usage(
        enabled=settings.usage_metering_enabled,
        provider=settings.get_embedding_provider_family(),
        model=settings.get_embedding_model_name(),
        doc_type=doc_task.doc_type,
        user_id=doc_task.user_id,
        index_mode=doc_task.index_mode,
        chunk_count=len(chunk_texts),
        token_count=dense_embed_tokens,
        total_chars=_meter_total_chars,
        page_count=(
            _meter_raw_page_count
            if isinstance(_meter_raw_page_count, int)
            and not isinstance(_meter_raw_page_count, bool)
            else None
        ),
        # bytes_ingested: raw source size at ingestion (raw WebDAV binary for
        # files, UTF-8 text size for text doc types — note, deck_card,
        # news_item, mail_message). See helper.
        bytes_ingested=ingested_byte_size(source.size if source else None, content),
        # bytes_stored: UTF-8 size of the chunk texts persisted as Qdrant payload
        # excerpts (includes chunk-overlap duplication).
        bytes_stored=sum(len(t.encode("utf-8")) for t in chunk_texts),
        # Tier that produced the parsed pages (registry stamps it on the result
        # metadata); text doc types stay "fast". Narrow defensively to str|None.
        pipeline_tier=(
            _meter_pipeline_tier if isinstance(_meter_pipeline_tier, str) else None
        ),
    )

    # Raw source size at ingestion (raw WebDAV binary for files, UTF-8 text size
    # for text doc types). Computed once here and reused for both the ingest-time
    # density metric and the per-point payload (payload_keys.SOURCE_BYTES) so the
    # current-corpus density snapshot can recompute chunks-per-MB from Qdrant.
    source_bytes = ingested_byte_size(source.size if source else None, content)

    # Observability-only cost signals (card #624), independent of USAGE_METERING.
    # Best-effort and self-contained in the helper so a metrics failure can never
    # disturb indexing.
    _record_ingest_vector_cost(
        doc_type=doc_task.doc_type,
        chunk_count=len(chunk_texts),
        source_bytes=source_bytes,
        dense_for_doc=dense_for_doc,
        overhead=settings.vector_ram_hnsw_overhead_factor,
    )

    # Prepare Qdrant points
    indexed_at = int(time.time())
    points = []

    # Decomposition payload keys (design §10.2) — written even in local mode so
    # a future migration to the external processor is friction-free. Computed
    # once per document (not per chunk). The local processor has no triage, so
    # PIPELINE_TIER is "fast"; ACL hash records at least the owner principal
    # (full share enumeration is a follow-up — a missing/partial acl_hash is
    # safe because the query-side pre-filter only applies when present + enabled).
    # Embedding identity stamped on every chunk point. Via the shared helper so it
    # is IDENTICAL to what the collection sentinel and the cross-user dedup lookup
    # produce (Deck #509). It records the dense embedding MODEL (so a model switch
    # forces a re-embed); it is orthogonal to keyword-vs-hybrid, which is tracked
    # separately by INDEX_MODE. Keyword points carry the model identity too — they
    # simply omit the dense vector — so a keyword doc dedups against the same
    # model-identity space (see claim_existing_index's monotonic rule).
    _embedding_identity = build_embedding_identity(settings)
    _acl_hash = compute_acl_hash([("user", doc_task.user_id)])

    # Observed-access ACL principals (computed once per document, not per chunk).
    # Seed with the indexer (and owner, if distinct). For files — the only type
    # with cross-user dedup and globally-unique IDs (Nextcloud fileid) — union in
    # any principals already recorded so re-indexing after a content change
    # preserves visibility for readers who had previously claimed the file. For
    # note/news_item/deck_card, IDs are per-user (not globally unique) and point
    # IDs are user-agnostic, so merging another user's principals on an ID
    # collision would wrongly cross-surface their content; those types are
    # seeded with the indexer only.
    _prior_principals = (
        await existing_principals(doc_task.doc_id, doc_task.doc_type)
        if doc_task.doc_type == "file"
        else []
    )
    _acl_principals = sorted(
        set(_prior_principals)
        | {
            f"user:{doc_task.user_id}",
            f"user:{doc_task.owner_id or doc_task.user_id}",
        }
    )

    # Surface deck card data quality issues at indexing time rather than
    # only at verification time (where _verify_deck_cards falls through to
    # legacy-data pass-through when board_id/stack_id are missing). This is
    # logged once per document — not per chunk — to avoid log spam.
    if doc_task.doc_type == "deck_card":
        missing_deck_fields = [
            field for field in ("board_id", "stack_id") if not file_metadata.get(field)
        ]
        if missing_deck_fields:
            logger.warning(
                "Indexing deck_card %s for user %s with missing metadata: %s; "
                "verification will fall back to legacy-data pass-through",
                doc_task.doc_id,
                doc_task.user_id,
                missing_deck_fields,
            )

    for i, (chunk, sparse_emb) in enumerate(zip(chunks, sparse_embeddings)):
        # Generate deterministic UUID for point ID
        # Using uuid5 with DNS namespace and combining doc info
        point_name = f"{doc_task.doc_type}:{doc_task.doc_id}:chunk:{i}"
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, point_name))

        # Keyword docs upsert sparse-only points; the loop is driven off
        # ``sparse_embeddings`` so the chunk count stays correct when
        # ``dense_embeddings`` is empty. See build_point_vector for the rationale.
        point_vector = build_point_vector(
            sparse_emb, dense_embeddings, i, dense_enabled=dense_for_doc
        )

        # Last page of a packed multi-page chunk (Deck #636); falls back to
        # page_number for single-page and char-path chunks so the citation
        # range is always set.
        page_end = resolve_page_end(chunk)

        points.append(
            PointStruct(
                id=point_id,
                vector=point_vector,
                payload={
                    "user_id": doc_task.user_id,
                    # owner_id is the UID of the file's owner — what
                    # search-time ACL expansion filters on. Today the scanner
                    # always runs as the file's owner (per-user crawl, only
                    # surfaces files the user owns or that fall under their
                    # WebDAV root), so owner_id == user_id is correct for
                    # every doc type indexed here. The fields are kept
                    # separate so a future indexer change that lets a user
                    # crawl shared-with-them content can set owner_id to the
                    # true owner without losing the "who indexed this" trail.
                    "owner_id": doc_task.owner_id or doc_task.user_id,
                    # Observed-access ACL set: every user whose scanner has seen
                    # (hence can read) this document. Seeded with the indexer (and
                    # owner, if distinct); grown lazily as other readers' scanners
                    # hit the tenant-wide dedup path. Search ORs a
                    # MatchAny(acl_principals, ["user:<me>"]) branch so a
                    # deduplicated shared file stays findable by every reader.
                    "acl_principals": _acl_principals,
                    "doc_id": doc_task.doc_id,
                    "doc_type": doc_task.doc_type,
                    "is_placeholder": False,  # Real indexed document (not placeholder)
                    "title": title,
                    "excerpt": chunk.text,  # Full chunk text (up to chunk_size, default 2048 chars)
                    "indexed_at": indexed_at,
                    "modified_at": doc_task.modified_at,
                    "etag": etag,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "chunk_start_offset": chunk.start_offset,
                    "chunk_end_offset": chunk.end_offset,
                    "metadata_version": 2,  # v2 includes position metadata
                    # Raw source size (bytes) at ingestion — the denominator the
                    # current-corpus density snapshot needs, previously discarded
                    # after embedding. Same on every chunk of the document.
                    payload_keys.SOURCE_BYTES: source_bytes,
                    # Decomposition payload keys (design §10.2), additive.
                    payload_keys.PROCESSOR_VERSION: "monolith-v1",
                    payload_keys.PARSED_AT: indexed_at,
                    # Actual tier that produced this doc (registry stamps it on
                    # the result metadata); non-PDF doc types stay "fast".
                    payload_keys.PIPELINE_TIER: file_metadata.get(
                        "pipeline_tier", "fast"
                    ),
                    payload_keys.EMBEDDING_IDENTITY: _embedding_identity,
                    # Per-document index mode: "hybrid" (this point carries a
                    # dense vector) or "keyword" (sparse-only). Drives verify-on-
                    # read tag selection, the dedup monotonic rule, and billing.
                    payload_keys.INDEX_MODE: doc_task.index_mode,
                    payload_keys.ACL_HASH: _acl_hash,
                    # File-specific metadata (PDF, etc.)
                    **(
                        {
                            "file_path": file_path,  # Store file path for retrieval
                            "mime_type": content_type,  # From WebDAV response
                            "file_size": file_metadata.get("file_size"),
                            "page_number": chunk.page_number,
                            "page_end": page_end,
                            "page_count": file_metadata.get("page_count"),
                            "author": file_metadata.get("author"),
                            "creation_date": file_metadata.get("creation_date"),
                            "has_images": file_metadata.get("has_images", False),
                            "image_count": file_metadata.get("image_count", 0),
                        }
                        if doc_task.doc_type == "file"
                        else {}
                    ),
                    # News item-specific metadata
                    **(
                        {
                            "feed_id": file_metadata.get("feed_id"),
                            "feed_title": file_metadata.get("feed_title"),
                            "author": file_metadata.get("author"),
                            "pub_date": file_metadata.get("pub_date"),
                            "starred": file_metadata.get("starred"),
                            "unread": file_metadata.get("unread"),
                            "url": file_metadata.get("url"),
                            "guid_hash": file_metadata.get("guid_hash"),
                            "enclosure_link": file_metadata.get("enclosure_link"),
                            "enclosure_mime": file_metadata.get("enclosure_mime"),
                        }
                        if doc_task.doc_type == "news_item"
                        else {}
                    ),
                    # Deck card-specific metadata
                    **(
                        {
                            "board_id": file_metadata.get("board_id"),
                            "board_title": file_metadata.get("board_title"),
                            "stack_id": file_metadata.get("stack_id"),
                            "stack_title": file_metadata.get("stack_title"),
                            "card_type": file_metadata.get("card_type"),
                            "duedate": file_metadata.get("duedate"),
                            "owner": file_metadata.get("owner"),
                        }
                        if doc_task.doc_type == "deck_card"
                        else {}
                    ),
                    # Mail message-specific metadata
                    **(
                        {
                            "subject": file_metadata.get("subject"),
                            "from": file_metadata.get("from"),
                            "to": file_metadata.get("to"),
                            "cc": file_metadata.get("cc"),
                            "bcc": file_metadata.get("bcc"),
                            "date_int": file_metadata.get("date_int"),
                            "has_attachments": file_metadata.get("has_attachments"),
                            "account_id": file_metadata.get("account_id"),
                            "mailbox_id": file_metadata.get("mailbox_id"),
                        }
                        if doc_task.doc_type == "mail_message"
                        else {}
                    ),
                    # Chunk bbox (PDF only) — normalized rectangles in [0,1]
                    # relative to page width/height. Replaces the legacy
                    # `highlighted_page_image` (Deck #76). The page number
                    # comes from `page_number` (set above for PDF chunks).
                    # ``bbox_source`` records provenance ("ocr" = gateway-provided
                    # surya geometry, "pymupdf" = local text-search).
                    **(
                        {"chunk_bbox": chunk_bboxes[i], "bbox_source": bbox_source}
                        if i in chunk_bboxes
                        else {}
                    ),
                },
            )
        )

    # A successful (re-)index supersedes any prior terminal failure: clear a
    # stale dead-letter marker (e.g. the file was fixed/replaced, or a new
    # escalation tier finally parsed it) so it isn't left behind. Only files are
    # ever dead-lettered, and only with a non-empty etag (is_dead_lettered
    # early-returns without one), so skip the extra Qdrant round-trip otherwise.
    # Cleared before the real-chunk upsert below: if that upsert then fails
    # transiently, the document is re-queued and re-parses once on the next scan
    # (an extra parse, never a silent drop) -- the safe ordering.
    if doc_task.doc_type == "file" and doc_task.etag:
        await clear_dead_letter(doc_task.doc_id, doc_task.doc_type)

    # Delete placeholder before writing real vectors
    # This prevents duplicates and cleans up the placeholder state
    try:
        await delete_placeholder_point(
            doc_id=doc_task.doc_id,
            doc_type=doc_task.doc_type,
            user_id=doc_task.user_id,
        )
    except Exception as e:
        # Log but don't fail indexing if placeholder deletion fails
        logger.warning(
            "Failed to delete placeholder for %s_%s: %s",
            doc_task.doc_type,
            doc_task.doc_id,
            e,
        )

    # Upsert to Qdrant in batches. Now that we no longer embed PNG payloads,
    # per-point payloads are small (chunk text + small metadata), so we can
    # safely use a larger batch size.
    BATCH_SIZE = 100
    with trace_operation(
        "vector_sync.qdrant_upsert",
        attributes={
            "vector_sync.point_count": len(points),
            "vector_sync.collection": settings.get_collection_name(),
            "vector_sync.bboxes_count": len(chunk_bboxes),
            "vector_sync.batch_size": BATCH_SIZE,
        },
    ):
        for batch_start in range(0, len(points), BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, len(points))
            batch = points[batch_start:batch_end]
            await qdrant_client.upsert(
                collection_name=settings.get_collection_name(),
                points=batch,
                wait=True,
            )
            if batch_end < len(points):
                logger.debug(
                    "Upserted batch %s/%s",
                    batch_start // BATCH_SIZE + 1,
                    (len(points) + BATCH_SIZE - 1) // BATCH_SIZE,
                )

    logger.info(
        "Indexed %s_%s for %s (%s chunks)",
        doc_task.doc_type,
        doc_task.doc_id,
        doc_task.user_id,
        len(chunks),
        extra={
            "doc_id": doc_task.doc_id,
            "doc_type": doc_task.doc_type,
            "chunks": len(chunks),
            "status": "success",
        },
    )
