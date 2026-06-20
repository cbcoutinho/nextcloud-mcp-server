"""
Prometheus metrics for the Nextcloud MCP Server.

This module defines all Prometheus metrics for monitoring server health, performance,
and resource usage. Metrics are organized by category:

- HTTP Server Metrics (RED: Rate, Errors, Duration)
- MCP Tool Metrics (per-tool invocation tracking)
- MCP Resource Metrics
- Nextcloud API Client Metrics
- OAuth Flow Metrics
- Vector Sync Metrics (conditional on feature flag)
- Database Operation Metrics
- External Dependency Health Metrics
"""

import functools
import logging
import time

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

from nextcloud_mcp_server.observability.tracing import trace_operation

logger = logging.getLogger(__name__)

# =============================================================================
# HTTP Server Metrics (RED + System)
# =============================================================================

http_requests_total = Counter(
    "mcp_http_requests_total",
    "Total HTTP requests received",
    ["method", "endpoint", "status_code"],
)

http_request_duration_seconds = Histogram(
    "mcp_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

http_requests_in_progress = Gauge(
    "mcp_http_requests_in_progress",
    "Number of HTTP requests currently being processed",
    ["method", "endpoint"],
)

# =============================================================================
# MCP Tool Metrics
# =============================================================================

mcp_tool_calls_total = Counter(
    "mcp_tool_calls_total",
    "Total MCP tool invocations",
    ["tool_name", "status"],  # status: success | error
)

mcp_tool_duration_seconds = Histogram(
    "mcp_tool_duration_seconds",
    "MCP tool execution duration in seconds",
    ["tool_name"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

mcp_tool_errors_total = Counter(
    "mcp_tool_errors_total",
    "Total MCP tool errors by type",
    ["tool_name", "error_type"],
)

# =============================================================================
# MCP Resource Metrics
# =============================================================================

mcp_resource_requests_total = Counter(
    "mcp_resource_requests_total",
    "Total MCP resource requests",
    ["resource_uri", "status"],
)

mcp_resource_duration_seconds = Histogram(
    "mcp_resource_duration_seconds",
    "MCP resource request duration in seconds",
    ["resource_uri"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# =============================================================================
# Nextcloud API Client Metrics
# =============================================================================

nextcloud_api_requests_total = Counter(
    "mcp_nextcloud_api_requests_total",
    "Total Nextcloud API requests",
    ["app", "method", "status_code"],  # app: notes, calendar, contacts, etc.
)

nextcloud_api_duration_seconds = Histogram(
    "mcp_nextcloud_api_duration_seconds",
    "Nextcloud API request duration in seconds",
    ["app", "method"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

nextcloud_api_retries_total = Counter(
    "mcp_nextcloud_api_retries_total",
    "Total Nextcloud API retries",
    ["app", "reason"],  # reason: 429 | timeout | connection_error
)

# =============================================================================
# OAuth Flow Metrics
# =============================================================================

oauth_token_validations_total = Counter(
    "mcp_oauth_token_validations_total",
    "Total OAuth token validation attempts",
    ["method", "result"],  # method: introspect | jwt; result: valid | invalid | error
)

oauth_token_cache_hits_total = Counter(
    "mcp_oauth_token_cache_hits_total",
    "Total OAuth token cache lookups",
    ["hit"],  # hit: true | false
)

oauth_refresh_token_operations_total = Counter(
    "mcp_oauth_refresh_token_operations_total",
    "Total refresh token storage operations",
    [
        "operation",
        "status",
    ],  # operation: store | retrieve | delete; status: success | error
)

# =============================================================================
# Vector Sync Metrics (optional feature)
# =============================================================================

vector_sync_documents_scanned_total = Counter(
    "mcp_vector_sync_documents_scanned_total",
    "Total documents scanned for vector sync",
)

vector_sync_documents_processed_total = Counter(
    "mcp_vector_sync_documents_processed_total",
    "Total documents processed for vector sync",
    ["status"],  # status: success | error
)

vector_sync_processing_duration_seconds = Histogram(
    "mcp_vector_sync_processing_duration_seconds",
    "Document processing duration in seconds",
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

vector_sync_queue_size = Gauge(
    "mcp_vector_sync_queue_size",
    "Current number of documents in processing queue",
)

# Outstanding ingest work (queued + in-flight), backend-agnostic. Published by
# the periodic vector_sync_metrics_task from ingest_status.get_ingest_pending(),
# so it is correct on every consumer path (single-user processor_task AND
# multi-user oauth_processor_task) and every queue backend (anyio buffer depth
# or procrastinate todo+doing) — unlike the per-loop update of
# ``vector_sync_queue_size``, which only ran on the single-user path.
vector_sync_pending_documents = Gauge(
    "mcp_vector_sync_pending_documents",
    "Outstanding ingest documents (queued or in-flight, not yet processed)",
)

# Corpus size in the vector store. ``indexed_documents`` counts distinct
# documents (one chunk_index=0 point per document); ``indexed_chunks`` counts
# every non-placeholder point. The two differ by the chunk fan-out (~N chunks
# per document), which is why a single "indexed" figure is ambiguous.
vector_sync_indexed_documents = Gauge(
    "mcp_vector_sync_indexed_documents",
    "Distinct documents indexed in the vector store (non-placeholder)",
)
vector_sync_indexed_chunks = Gauge(
    "mcp_vector_sync_indexed_chunks",
    "Total indexed chunks (non-placeholder points) in the vector store",
)

# Per-tier-queue ingest depth (Deck #323). One series per (queue, status) so an
# operator can see where work sits -- a ``fast`` backlog, docs waiting on
# ``ingest-structured``/``ingest-ocr``, or failures piling up per tier. KEDA
# scales each tier Deployment off the queue's ``todo`` depth via direct SQL; this
# gauge is the dashboard/alerting view of the same figures. Published by the
# periodic vector_sync metrics task from the procrastinate per-queue job counts.
ingest_queue_depth = Gauge(
    "astrolabe_ingest_queue_depth",
    "Ingest jobs per tier queue by status (todo/doing/failed)",
    ["queue", "status"],
)
# The subset of statuses worth a gauge series; the rest (succeeded/cancelled/
# aborted) are pruned from the queue table and uninteresting for operating.
_INGEST_DEPTH_STATUSES = ("todo", "doing", "failed")

qdrant_operations_total = Counter(
    "mcp_qdrant_operations_total",
    "Total Qdrant vector database operations",
    [
        "operation",
        "status",
    ],  # operation: upsert | search | delete; status: success | error
)

# =============================================================================
# Astrolabe Document-Processing Pipeline Metrics
# =============================================================================
#
# Product-signal metrics for the document-processing pipeline
# (scan -> fetch -> parse -> chunk -> embed -> Qdrant upsert). These use the
# ``astrolabe_`` prefix to distinguish the indexing/product pipeline from the
# ``mcp_`` protocol metrics above. The tenant dimension is NOT a label here --
# it is supplied by the Kubernetes ``namespace`` label at scrape time.
#
# Tiered-pipeline readiness: ``processor`` and ``tier`` are labels from day one
# so that adding new extraction tiers (docling, OCR, LLM) later is purely
# additive (new label values), never new metric names.
#   tier vocabulary (escalation ladder): fast -> structured -> ocr -> llm
#
# Cardinality rule: ``mime_type`` and embedding ``model`` are span attributes
# only, never metric labels.

# --- Parse tier (recorded at the ProcessorRegistry.process() boundary) --------

document_parse_duration_seconds = Histogram(
    "astrolabe_document_parse_duration_seconds",
    "Document text-extraction (parse) duration in seconds",
    ["processor", "tier", "status"],  # status: success | error
    # Buckets reach 300s: large PDFs exceed the 60s ceiling of the whole-doc
    # histogram, which would otherwise pile every large parse into +Inf.
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

document_parse_total = Counter(
    "astrolabe_document_parse_total",
    "Total document parse attempts",
    ["processor", "tier", "status"],  # status: success | error
)

document_pages_processed_total = Counter(
    "astrolabe_document_pages_processed_total",
    "Total document pages processed (page-rate signal)",
    ["processor", "tier"],
)

document_chars_processed_total = Counter(
    "astrolabe_document_chars_processed_total",
    "Total characters extracted from documents",
    ["processor", "tier"],
)

document_bytes_processed_total = Counter(
    "astrolabe_document_bytes_processed_total",
    "Total bytes of source documents parsed",
    ["processor", "tier"],
)

# --- Escalation (tiered-pipeline readiness; ~0 until extra tiers exist) --------

document_escalation_total = Counter(
    "astrolabe_document_escalation_total",
    "Total document parse escalations between tiers",
    # reason: low_confidence | empty_text | corrupt_glyphs | unsupported | error | forced
    ["from_tier", "to_tier", "reason"],
)

# Would-be escalations SUPPRESSED because the target tier is disabled (Deck
# #324). The cost-sensitive ``ocr`` tier is opt-in (DOCUMENT_OCR_ENABLED): when
# it's off, a doc the classifier would route to OCR is indexed at the pre-OCR
# tier instead of hopping, and that intent is counted here rather than on
# document_escalation_total. This is the "what-if OCR were enabled" signal —
# escalation_suppressed_total{to_tier="ocr"} is the latent OCR demand an operator
# weighs before enabling OCR; enabling it converts these into real
# document_escalation_total{to_tier="ocr"} hops.
document_escalation_suppressed_total = Counter(
    "astrolabe_document_escalation_suppressed_total",
    "Would-be tier escalations suppressed because the target tier is disabled",
    # reason: low_confidence | empty_text | corrupt_glyphs. (corrupt_glyphs lands
    # here only in the narrow case where the structured tier is unregistered AND
    # OCR is registered-but-disabled: evaluate_escalation follows minimum="structured"
    # past the missing rung to OCR, which is gated off -> suppressed{to_tier="ocr"}.)
    ["from_tier", "to_tier", "reason"],
)

# Hard parse failures: the parse now runs in an isolated subprocess, so a
# timeout/OOM that kills the worker is caught here. This is distinct from
# ``document_parse_total{status="error"}`` (an in-process exception): a hard
# OOM previously killed the pod before any except ran, so it incremented
# nothing -- this counter makes those failures visible.
document_parse_failed_total = Counter(
    "astrolabe_document_parse_failed_total",
    "Document parses that failed in the isolated worker (process killed)",
    ["reason"],  # reason: timeout | oom | error
)

# Documents dead-lettered after a terminal parse failure: the failing tier had
# no higher escalation tier available (e.g. structured timed out with OCR off),
# so the document is recorded as permanently failed for this content-version and
# stops being re-queued (vector/dead_letter.py). Distinct from
# ``document_parse_failed_total`` (which counts every failed parse attempt,
# including the ones that will be retried) -- this fires once when a document
# is given up on, and clears implicitly when its etag or the escalation-tier set
# changes and it is re-attempted.
document_dead_lettered_total = Counter(
    "astrolabe_document_dead_lettered_total",
    "Documents dead-lettered after a terminal parse failure (no escalation tier)",
    ["reason"],  # reason: timeout | oom | error | oversize
)

# Documents dropped after exhausting in-process indexing retries (the scanner
# re-picks them on a later full scan, so this is "dropped for this cycle", not
# "lost forever"). Labelled by classified cause so the embed-drop rate from a
# transient backend-pod rollover (connection/timeout) is alertable distinctly
# from a persistent fault (card 309).
vector_ingest_dropped_total = Counter(
    "astrolabe_vector_ingest_dropped_total",
    "Documents dropped after exhausting indexing retries, by cause",
    # reason: connection | timeout | rate_limit | server | qdrant | other
    ["reason"],
)

# --- Tier-0 classifier (shadow mode) -----------------------------------------
#
# The classifier runs a cheap pre-pass per PDF and recommends a starting tier.
# In shadow mode it changes no routing -- these metrics gather the per-tenant
# doc-mix needed to tune the thresholds before routing is enabled.

document_classified_total = Counter(
    "astrolabe_document_classified_total",
    "Documents classified by tier-0, by recommended starting tier",
    ["recommended_tier"],  # fast | ocr
)

document_classifier_flag_total = Counter(
    # Diagnostic flags, independent of the routing verdict: image_heavy fires if
    # ANY page is image-heavy whereas the ocr route needs a fraction of pages,
    # so flag{image_heavy} is expected to exceed classified{recommended_tier=ocr}.
    "astrolabe_document_classifier_flag_total",
    "Tier-0 classifier flags raised on documents",
    ["flag"],  # image_heavy | scanned | bad_text_layer | corrupt_glyphs
)

document_text_quality = Histogram(
    "astrolabe_document_text_quality",
    "Tier-0 mean text-layer quality per document (0=junk, 1=clean prose)",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# Per-document fraction of OCR-worthy pages (near-empty / junk-quality / scanned).
# This is the value the DOCUMENT_OCR_PAGE_FRACTION threshold acts on, so its
# distribution per tenant is the lever for tuning OCR escalation (quality vs
# cost): how many docs sit just below/above the cutoff. Pair with
# document_text_quality (where to set the per-page quality floor) and
# document_escalation_total (realized OCR volume).
document_ocr_page_fraction = Histogram(
    "astrolabe_document_ocr_page_fraction",
    "Tier-0 fraction of OCR-worthy pages per document (0=all-clean, 1=all-bad)",
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# --- Embedding stages ---------------------------------------------------------

embedding_duration_seconds = Histogram(
    "astrolabe_embedding_duration_seconds",
    "Embedding batch duration in seconds",
    ["kind", "provider", "status"],  # kind: dense | sparse
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

embedding_requests_total = Counter(
    "astrolabe_embedding_requests_total",
    "Total embedding batch calls",
    ["kind", "provider", "status"],  # one per embed_batch / encode_batch call
)

embedding_chunks_total = Counter(
    "astrolabe_embedding_chunks_total",
    "Total chunks embedded",
    ["kind", "provider"],
)

embedding_chars_total = Counter(
    "astrolabe_embedding_chars_total",
    "Total characters embedded",
    ["kind", "provider"],
)

# Token consumption — the billed cost unit (mirrors the tokens_embedded billing
# measure, Deck #67). On a dedicated counter (not folded into the chunk/request
# metrics above) so query embeds don't inflate indexing dashboards; labelled by
# operation = index | query. Always emitted, independent of USAGE_METERING_ENABLED.
#
# Dashboard note: operation="query" is recorded at embed time (before Qdrant /
# verify-on-read), whereas the billing-store tokens_embedded row is written only
# after the search fully succeeds. So this counter can legitimately exceed the
# billing aggregate when a search fails post-embed — don't alert on that gap as
# a divergence bug.
embedding_tokens_total = Counter(
    "astrolabe_embedding_tokens_total",
    "Total embedding tokens consumed (provider-reported or estimated)",
    ["provider", "operation"],  # operation: index | query
)

# --- Chunking & indexed-by-type -----------------------------------------------

document_chunks_total = Counter(
    "astrolabe_document_chunks_total",
    "Total chunks produced by the chunker",
    ["doc_type"],
)

documents_indexed_total = Counter(
    "astrolabe_documents_indexed_total",
    "Total documents indexed, by source type",
    ["source", "status"],  # source: note | file | deck_card | news_item
)

# --- Document discovery / coverage ------------------------------------------
#
# Fires when a paged WebDAV SEARCH (folder-expansion during a scan) hits the
# WEBDAV_SEARCH_MAX_RESULTS ceiling, meaning the discovered file set was capped
# and some tagged documents may never be queued for indexing. This is the
# alertable signal that prevents the old *silent* 100-result truncation from
# recurring. Tenant is the Kubernetes ``namespace`` label, as elsewhere.
document_scan_truncated_total = Counter(
    "astrolabe_document_scan_truncated_total",
    "Times a folder-expansion SEARCH hit the result ceiling (coverage truncated)",
)

# =============================================================================
# Database Metrics
# =============================================================================

db_operations_total = Counter(
    "mcp_db_operations_total",
    "Total database operations",
    ["db", "operation", "status"],  # db: sqlite | qdrant; operation varies
)

db_operation_duration_seconds = Histogram(
    "mcp_db_operation_duration_seconds",
    "Database operation duration in seconds",
    ["db", "operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# =============================================================================
# External Dependency Health Metrics
# =============================================================================

dependency_health = Gauge(
    "mcp_dependency_health",
    "External dependency health status (1=up, 0=down)",
    ["dependency"],  # dependency: nextcloud | keycloak | qdrant | unstructured
)

dependency_check_duration_seconds = Histogram(
    "mcp_dependency_check_duration_seconds",
    "Dependency health check duration in seconds",
    ["dependency"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# =============================================================================
# Metrics Setup and HTTP Handler
# =============================================================================


def setup_metrics(port: int = 9090) -> None:
    """
    Initialize Prometheus metrics collection and start HTTP server.

    Starts a dedicated HTTP server on the specified port to serve metrics.
    This server runs in a separate thread and is isolated from the main application.

    Args:
        port: Port to serve metrics on (default: 9090)

    Note:
        Metrics endpoint (/metrics) is ONLY accessible on this dedicated port,
        not on the main application HTTP port. This is a security best practice
        to prevent external exposure of metrics.
    """
    try:
        start_http_server(port)
        logger.info("Prometheus metrics server started on port %s", port)
    except OSError as e:
        if "Address already in use" in str(e):
            logger.warning(
                "Metrics port %s already in use (metrics server likely already running)",
                port,
            )
        else:
            logger.error("Failed to start metrics server on port %s: %s", port, e)
            raise


# =============================================================================
# Convenience Functions for Common Metric Updates
# =============================================================================


def record_tool_call(tool_name: str, duration: float, status: str = "success") -> None:
    """
    Record metrics for an MCP tool call.

    Args:
        tool_name: Name of the MCP tool
        duration: Execution duration in seconds
        status: "success" or "error"
    """
    mcp_tool_calls_total.labels(tool_name=tool_name, status=status).inc()
    mcp_tool_duration_seconds.labels(tool_name=tool_name).observe(duration)


def record_tool_error(tool_name: str, error_type: str) -> None:
    """
    Record an MCP tool error.

    Args:
        tool_name: Name of the MCP tool
        error_type: Type of error (e.g., "HTTPStatusError", "ValueError")
    """
    mcp_tool_errors_total.labels(tool_name=tool_name, error_type=error_type).inc()


def record_nextcloud_api_call(
    app: str,
    method: str,
    status_code: int,
    duration: float,
) -> None:
    """
    Record metrics for a Nextcloud API call.

    Args:
        app: Nextcloud app name (notes, calendar, contacts, etc.)
        method: HTTP method (GET, POST, PUT, DELETE, PROPFIND, etc.)
        status_code: HTTP status code
        duration: Request duration in seconds
    """
    nextcloud_api_requests_total.labels(
        app=app, method=method, status_code=str(status_code)
    ).inc()
    nextcloud_api_duration_seconds.labels(app=app, method=method).observe(duration)


def record_nextcloud_api_retry(app: str, reason: str) -> None:
    """
    Record a Nextcloud API retry.

    Args:
        app: Nextcloud app name
        reason: Retry reason (429, timeout, connection_error)
    """
    nextcloud_api_retries_total.labels(app=app, reason=reason).inc()


def record_oauth_token_validation(method: str, result: str) -> None:
    """
    Record an OAuth token validation.

    Args:
        method: Validation method ("introspect" or "jwt")
        result: Validation result ("valid", "invalid", or "error")
    """
    oauth_token_validations_total.labels(method=method, result=result).inc()


def record_db_operation(
    db: str, operation: str, duration: float, status: str = "success"
) -> None:
    """
    Record a database operation.

    Args:
        db: Database type ("sqlite" or "qdrant")
        operation: Operation type (e.g., "insert", "select", "upsert", "search")
        duration: Operation duration in seconds
        status: "success" or "error"
    """
    db_operations_total.labels(db=db, operation=operation, status=status).inc()
    db_operation_duration_seconds.labels(db=db, operation=operation).observe(duration)


def set_dependency_health(dependency: str, is_healthy: bool) -> None:
    """
    Update external dependency health status.

    Args:
        dependency: Dependency name (nextcloud, keycloak, qdrant, unstructured)
        is_healthy: True if dependency is healthy, False otherwise
    """
    dependency_health.labels(dependency=dependency).set(1 if is_healthy else 0)


def record_dependency_check(dependency: str, duration: float) -> None:
    """
    Record a dependency health check duration.

    Args:
        dependency: Dependency name
        duration: Check duration in seconds
    """
    dependency_check_duration_seconds.labels(dependency=dependency).observe(duration)


def record_vector_sync_scan(documents_found: int) -> None:
    """
    Record documents scanned during vector sync.

    Args:
        documents_found: Number of documents discovered in scan
    """
    vector_sync_documents_scanned_total.inc(documents_found)


def record_vector_sync_processing(
    duration: float, status: str = "success", doc_type: str | None = None
) -> None:
    """
    Record document processing with duration and status.

    Args:
        duration: Processing duration in seconds
        status: "success" or "error"
        doc_type: Optional document source type (note, file, deck_card,
            news_item). When supplied, also increments the per-type
            ``astrolabe_documents_indexed_total`` counter. The legacy
            ``mcp_vector_sync_documents_processed_total`` counter is always
            incremented for backward compatibility.
    """
    vector_sync_documents_processed_total.labels(status=status).inc()
    vector_sync_processing_duration_seconds.observe(duration)
    if doc_type is not None:
        documents_indexed_total.labels(source=doc_type, status=status).inc()


def record_qdrant_operation(operation: str, status: str = "success") -> None:
    """
    Record Qdrant vector database operation.

    Args:
        operation: Operation type ("upsert", "search", "delete")
        status: "success" or "error"
    """
    qdrant_operations_total.labels(operation=operation, status=status).inc()


def update_vector_sync_queue_size(size: int) -> None:
    """
    Update vector sync queue size gauge.

    Args:
        size: Current queue size
    """
    vector_sync_queue_size.set(size)


def update_vector_sync_pending_documents(count: int) -> None:
    """Set the outstanding-ingest-work gauge (queued + in-flight documents)."""
    vector_sync_pending_documents.set(count)


def update_vector_sync_indexed_documents(count: int) -> None:
    """Set the distinct-indexed-documents gauge."""
    vector_sync_indexed_documents.set(count)


def update_vector_sync_indexed_chunks(count: int) -> None:
    """Set the total-indexed-chunks gauge."""
    vector_sync_indexed_chunks.set(count)


def update_ingest_queue_depth(by_queue: dict[str, dict[str, int]] | None) -> None:
    """Set the per-tier-queue depth gauge from procrastinate job counts (#323).

    ``by_queue`` is ``{queue_name: {status: count}}`` (see
    ``queue.procrastinate.get_ingest_job_counts_by_queue``). No-op only on the
    memory backend (``by_queue is None``); an empty dict (postgres backend with
    every queue drained) still runs the pre-zero so the gauge reads 0.

    Every managed queue is zeroed first: ``list_queues_async`` stops returning a
    queue once it has no jobs, so a queue that drained to empty drops out of
    ``by_queue`` entirely (and when ALL drain, ``by_queue`` is ``{}``). Without
    the pre-zero its gauge series would stick at its last non-zero value (ghost
    backlog in Grafana/alerts) instead of reading 0. The live counts then
    overwrite the zeros for queues that still have work.
    """
    # ``is None`` not ``not by_queue``: an empty dict means "postgres, all queues
    # drained" and MUST still zero the gauge -- only None (memory) is the no-op.
    if by_queue is None:
        return
    # Lazy import to keep observability decoupled from the queue layer at module
    # load (and sidestep any import cycle); both names are public constants.
    from nextcloud_mcp_server.vector.queue.procrastinate import (  # noqa: PLC0415
        ALL_INGEST_QUEUES,
        LEGACY_INGEST_QUEUE,
        LEGACY_INGEST_QUEUE_OCR_INCLUSTER,
        LEGACY_INGEST_QUEUE_OCR_UPSTREAM,
    )

    for queue in (
        *ALL_INGEST_QUEUES,
        LEGACY_INGEST_QUEUE,
        LEGACY_INGEST_QUEUE_OCR_INCLUSTER,
        LEGACY_INGEST_QUEUE_OCR_UPSTREAM,
    ):
        for status in _INGEST_DEPTH_STATUSES:
            ingest_queue_depth.labels(queue=queue, status=status).set(0)
    for queue, per_status in by_queue.items():
        for status in _INGEST_DEPTH_STATUSES:
            ingest_queue_depth.labels(queue=queue, status=status).set(
                per_status.get(status, 0)
            )


def record_document_parse(
    processor: str,
    tier: str,
    duration: float,
    pages: int = 0,
    chars: int = 0,
    byte_size: int = 0,
    status: str = "success",
) -> None:
    """
    Record a document parse (text extraction) at the processor boundary.

    Args:
        processor: Processor name (e.g. "pymupdf", "unstructured", "tesseract")
        tier: Extraction tier (fast | structured | ocr | llm)
        duration: Parse duration in seconds
        pages: Number of pages parsed (0 if not page-based)
        chars: Number of characters extracted
        byte_size: Size of the source document in bytes
        status: "success" or "error"
    """
    document_parse_duration_seconds.labels(
        processor=processor, tier=tier, status=status
    ).observe(duration)
    document_parse_total.labels(processor=processor, tier=tier, status=status).inc()
    # Throughput counters (pages/chars/bytes) accrue only on a full success.
    # A partial extraction flagged success=False is recorded above as a
    # parse-error but is intentionally excluded here so low-confidence output
    # never inflates pipeline throughput.
    if status == "success":
        if pages > 0:
            document_pages_processed_total.labels(processor=processor, tier=tier).inc(
                pages
            )
        if chars > 0:
            document_chars_processed_total.labels(processor=processor, tier=tier).inc(
                chars
            )
        if byte_size > 0:
            document_bytes_processed_total.labels(processor=processor, tier=tier).inc(
                byte_size
            )


def record_document_escalation(from_tier: str, to_tier: str, reason: str) -> None:
    """
    Record a document parse escalation between tiers.

    Args:
        from_tier: Tier that could not satisfactorily parse the document
        to_tier: Tier the document was escalated to
        reason: low_confidence | empty_text | corrupt_glyphs | unsupported | error | forced
    """
    document_escalation_total.labels(
        from_tier=from_tier, to_tier=to_tier, reason=reason
    ).inc()


def record_document_escalation_suppressed(
    from_tier: str, to_tier: str, reason: str
) -> None:
    """Record a would-be escalation suppressed because ``to_tier`` is disabled.

    The "what-if OCR were enabled" signal (Deck #324): the document is indexed at
    ``from_tier`` (terminal) rather than hopped, because the ideal next tier
    (typically ``ocr``) is turned off. See ``document_escalation_suppressed_total``.
    """
    document_escalation_suppressed_total.labels(
        from_tier=from_tier, to_tier=to_tier, reason=reason
    ).inc()


def record_document_parse_failed(reason: str) -> None:
    """Record a hard parse failure from the isolated worker.

    Args:
        reason: ``timeout`` | ``oom`` | ``error``
    """
    document_parse_failed_total.labels(reason=reason).inc()


def record_document_dead_lettered(reason: str) -> None:
    """Record a document dead-lettered after a terminal parse failure.

    Counts the dead-letter *attempt*: it is incremented alongside the
    ``mark_dead_letter`` call, which is fail-safe (a Qdrant write error is logged,
    not raised), so a transient write failure can leave this counter marginally
    above the live marker count in Qdrant.

    Args:
        reason: ``timeout`` | ``oom`` | ``error`` (the terminal parse failure
            reason carried from the isolated worker) or ``oversize`` (rejected by
            the pre-parse size guard, which no tier can ever parse).
    """
    document_dead_lettered_total.labels(reason=reason).inc()


def record_ingest_dropped(reason: str) -> None:
    """Record a document dropped after exhausting in-process indexing retries.

    Args:
        reason: ``connection`` | ``timeout`` | ``rate_limit`` | ``server`` |
            ``qdrant`` | ``other`` (classified from the terminal exception).
    """
    vector_ingest_dropped_total.labels(reason=reason).inc()


def record_document_classification(
    recommended_tier: str,
    flags: set[str],
    mean_text_quality: float,
    ocr_page_fraction: float = 0.0,
) -> None:
    """Record a tier-0 classification result.

    Primitive args (not the DocClassification object) keep the observability
    layer free of a dependency on document_processors. ``mean_text_quality`` and
    ``ocr_page_fraction`` feed the two histograms operators use to tune the OCR
    escalation thresholds per tenant (quality vs cost).
    """
    document_classified_total.labels(recommended_tier=recommended_tier).inc()
    for flag in flags:
        document_classifier_flag_total.labels(flag=flag).inc()
    document_text_quality.observe(mean_text_quality)
    document_ocr_page_fraction.observe(ocr_page_fraction)


def record_embedding(
    kind: str,
    provider: str,
    duration: float,
    chunks: int = 0,
    chars: int = 0,
    status: str = "success",
) -> None:
    """
    Record an embedding batch call.

    Args:
        kind: "dense" or "sparse"
        provider: Provider family (bedrock | openai | mistral | ollama | simple
            for dense; "bm25" for sparse)
        duration: Batch duration in seconds
        chunks: Number of chunks embedded
        chars: Total characters embedded
        status: "success" or "error"
    """
    embedding_duration_seconds.labels(
        kind=kind, provider=provider, status=status
    ).observe(duration)
    embedding_requests_total.labels(kind=kind, provider=provider, status=status).inc()
    if status == "success":
        if chunks > 0:
            embedding_chunks_total.labels(kind=kind, provider=provider).inc(chunks)
        if chars > 0:
            embedding_chars_total.labels(kind=kind, provider=provider).inc(chars)


def record_embedding_tokens(provider: str, operation: str, tokens: int) -> None:
    """Export embedding token consumption to Prometheus.

    Mirrors the ``tokens_embedded`` billing measure (Deck #67) as an always-on
    observability signal — emitted regardless of ``USAGE_METERING_ENABLED`` so
    OSS/self-host deployments still see token cost in Grafana.

    Args:
        provider: Provider family (mistral | openai | bedrock | ollama | simple).
        operation: ``"index"`` (chunk-batch embedding) or ``"query"`` (search
            query embedding).
        tokens: Token count for this embedding request (no-op when ``<= 0``).
    """
    if tokens > 0:
        embedding_tokens_total.labels(provider=provider, operation=operation).inc(
            tokens
        )


def record_document_chunks(doc_type: str, count: int) -> None:
    """
    Record the number of chunks produced for a document.

    Args:
        doc_type: Document source type (note, file, deck_card, news_item)
        count: Number of chunks produced
    """
    document_chunks_total.labels(doc_type=doc_type).inc(count)


# =============================================================================
# Decorator for Automatic Tool Instrumentation
# =============================================================================


def instrument_tool(func):
    """
    Decorator to automatically instrument MCP tool functions with metrics and tracing.

    Wraps async tool functions to record execution time, success/error status, and
    create OpenTelemetry trace spans. Compatible with @mcp.tool() and @require_scopes()
    decorators.

    Usage:
        @mcp.tool()
        @require_scopes("notes.write")
        @instrument_tool
        async def nc_notes_create_note(...):
            ...

    Args:
        func: The async function to instrument

    Returns:
        Wrapped function with metrics and tracing instrumentation
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        tool_name = func.__name__
        start_time = time.time()

        # Extract tool arguments for tracing (sanitize sensitive fields)
        # kwargs contains the actual arguments passed to the tool
        tool_args = {
            k: v
            for k, v in kwargs.items()
            if k not in ("password", "token", "secret", "api_key", "etag", "ctx")
        }

        # Create trace span with metrics collection
        with trace_operation(
            f"mcp.tool.{tool_name}",
            attributes={
                "mcp.tool.name": tool_name,
                "mcp.tool.args": str(tool_args)[:500]
                if tool_args
                else None,  # Limit to 500 chars
            },
            record_exception=True,
        ):
            try:
                result = await func(*args, **kwargs)
                duration = time.time() - start_time
                record_tool_call(tool_name, duration, "success")
                return result
            except Exception as e:
                duration = time.time() - start_time
                record_tool_call(tool_name, duration, "error")
                record_tool_error(tool_name, type(e).__name__)
                raise

    return wrapper
