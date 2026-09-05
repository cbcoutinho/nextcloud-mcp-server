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
import threading
import time
from collections.abc import Mapping
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.context import CallNext, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)
from prometheus_client.core import GaugeHistogramMetricFamily
from prometheus_client.registry import Collector

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
# MCP Client Fleet Metrics
# =============================================================================
#
# Baseline for the mcp python-sdk 1.x -> 2.x (protocol 2026-07-28) upgrade,
# whose two most consequential changes are silent: elicitation loses its
# back-channel, and an MCPError raised in a tool stops becoming
# CallToolResult(is_error=True) and becomes a JSON-RPC error instead. Neither
# raises, neither shows up in existing metrics. These four record the fleet
# composition and delivery semantics so the change is visible as a step in a
# graph rather than a bug report.
#
# Post-upgrade, what each of the two now reads as:
#   - elicitation: mcp_elicitation_total{outcome="message_only",
#     reason="no_back_channel"} rising against reason="accepted" is 2026-era
#     clients arriving. There is no back-channel to restore; the login URL
#     travels in the returned message instead.
#   - MCPError: NextcloudMCPServer.call_tool maps it back to ToolError, so
#     mcp_tool_outcomes_total{outcome="protocol_error"} should stay at zero.
#     A non-zero reading means something bypassed that boundary.

mcp_client_sessions_total = Counter(
    "mcp_client_sessions_total",
    "MCP sessions observed, by client identity and negotiated protocol version",
    ["client_name", "client_version", "protocol_version"],
)

mcp_client_capability = Gauge(
    "mcp_client_capability",
    "1 if this client's most recent session declared the capability, else 0",
    ["client_name", "capability"],
)

mcp_elicitation_total = Counter(
    "mcp_elicitation_total",
    "Elicitation prompt outcomes; reason splits the message_only fallback causes",
    ["prompt", "outcome", "reason"],
)

mcp_tool_outcomes_total = Counter(
    "mcp_tool_outcomes_total",
    "How the MCP SDK delivered each tool call: success | tool_error "
    "(CallToolResult.is_error, the model sees the message) | protocol_error "
    "(JSON-RPC error, the model does not). tool_name here is the tool's "
    "*registered* MCP name, while mcp_tool_calls_total uses the decorated "
    "function's __name__. Every tool function is named after the tool it "
    "registers, so the two agree — test_tool_call_logging.py guards that.",
    ["tool_name", "outcome"],
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
    "OAuth token validation attempts. `reason` is why a non-valid result "
    "happened — without it a rejection is uninterpretable, and a rejection is "
    "what forces an MCP client's user to log in again. `client_id` attributes "
    "it, so 'which client is being disconnected, and why' is one query.",
    # CANONICAL vocabulary — this is the one place the values are enumerated in
    # code. When adding a rejection path, update this list, then
    # UnifiedTokenVerifier._OUR_FAULT_REASONS (which decides `result`) and
    # docs/observability.md (which carries the alert queries). Do not re-list
    # them in docstrings: a third copy drifted out of sync inside a single PR.
    # method: jwt | introspect | userinfo | allowlist | unknown
    # result: valid | invalid (caller's token) | error (ours) — derived from
    #         reason in _reject(), never set independently
    # reason: none (valid) | expired | inactive | bad_signature | bad_issuer
    #         | bad_audience | not_allowlisted | not_configured
    #         | network_error | no_signing_keys | unknown
    ["method", "result", "reason", "client_id"],
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

oauth_grants_total = Counter(
    "mcp_oauth_grants_total",
    "OAuth grants processed by the AS proxy token endpoint. `refresh_token` "
    "records whether the grant produced one — a client that never receives a "
    "refresh token must re-run the full authorization flow, which is a "
    "user-visible re-login, so `grant_type=authorization_code` repeating for "
    "one client is the signature of the disconnect being investigated.",
    # grant_type: authorization_code | refresh_token | unsupported
    # result: success | error
    # refresh_token: issued | absent | unknown (unknown = the grant failed, so
    #   there was no response to inspect)
    ["grant_type", "result", "refresh_token"],
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

# Documents parked as permanently-failed, published as a GAUGE from the
# long-lived backend rather than left to the worker-side counters.
#
# ``astrolabe_document_dead_lettered_total`` increments in the ingest worker,
# which is the wrong place to alert from on two counts: the worker container is
# not a Prometheus scrape target (only the backend Pod is), and KEDA scales the
# ingest tiers 0<->1, so a counter that fires and then terminates may never be
# scraped at all and resets on every scale-up. Four dead-letter events in a day
# produced no time series whatsoever (Deck #911).
#
# Dead-lettering is *durable state* in Qdrant (a tombstone point per document),
# not just an event, so a gauge read back from the collection is both the honest
# representation and immune to Pod lifecycle. Alert on ``> 0`` or on an increase
# over a window; it falls back to 0 when the tombstones are cleared or their
# etag/tier signature changes and the documents are re-attempted.
vector_sync_dead_lettered_documents = Gauge(
    "mcp_vector_sync_dead_lettered_documents",
    "Documents parked as permanently-failed (dead-letter tombstones in Qdrant)",
)

# Dense-vector RAM footprint of the collection — the real cost driver for hybrid
# search (billing is on source bytes, not vector RAM). Two views published by the
# periodic vector_sync metrics task so operators can watch the "density risk"
# (dense/low-fill docs pull chunk-per-byte high) and validate the estimate:
#   * ``estimated_vector_bytes`` — deterministic, from OUR hybrid chunk count:
#     ``hybrid_chunks * dim * 4 (float32) * hnsw_overhead``. Keyword-index chunks
#     are sparse-only and carry no dense vector, so they contribute nothing.
#   * ``qdrant_vectors`` / ``qdrant_vector_bytes`` — ground truth from Qdrant's own
#     reported ``vectors_count`` (via ``get_collection``), converted with the same
#     dim/overhead. The estimate-vs-actual gap catches duplication/segment drift.
vector_sync_estimated_vector_bytes = Gauge(
    "mcp_vector_sync_estimated_vector_bytes",
    "Estimated dense-vector RAM footprint (hybrid_chunks * dim * 4 * hnsw_overhead)",
)
vector_sync_qdrant_vectors = Gauge(
    "mcp_vector_sync_qdrant_vectors",
    "Dense vectors reported by Qdrant get_collection().vectors_count",
)
vector_sync_qdrant_vector_bytes = Gauge(
    "mcp_vector_sync_qdrant_vector_bytes",
    "Estimated dense-vector RAM from Qdrant vectors_count (vectors * dim * 4 * hnsw_overhead)",
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
    ["processor", "tier", "status"],  # status: success | error | pending
    # Buckets reach 300s: large PDFs exceed the 60s ceiling of the whole-doc
    # histogram, which would otherwise pile every large parse into +Inf.
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

document_parse_total = Counter(
    "astrolabe_document_parse_total",
    "Total document parse attempts",
    ["processor", "tier", "status"],  # status: success | error | pending
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

# Size distribution of everything a tenant offers for ingest, observed BEFORE the
# oversize gate so the over-cap tail -- the part that decides caps, spool sizing
# and worker memory -- is visible rather than silently dropped. Sizing decisions
# were previously made from a one-off manual crawl (866 files / 66.84 GB, 51%
# over cap); this makes the same picture a query for every tenant.
#
# Buckets are exponential to 2 GiB: an observed corpus spanned 1 MB to 1040 MB,
# so the tail buckets carry real signal and must not collapse into +Inf.
# Deliberately NOT labelled by tenant -- Prometheus already attaches the
# namespace/pod, and a tenant label here would multiply series per bucket.
document_ingest_size_bytes = Histogram(
    "astrolabe_document_ingest_size_bytes",
    "Source size of documents offered for ingest, before the oversize gate",
    ["doc_type"],
    buckets=(
        64 * 1024,
        256 * 1024,
        1024 * 1024,
        4 * 1024 * 1024,
        16 * 1024 * 1024,
        32 * 1024 * 1024,
        64 * 1024 * 1024,
        128 * 1024 * 1024,
        256 * 1024 * 1024,
        512 * 1024 * 1024,
        1024 * 1024 * 1024,
        2048 * 1024 * 1024,
    ),
)

document_ingest_rejected_total = Counter(
    "astrolabe_document_ingest_rejected_total",
    "Documents rejected before parsing, by reason",
    ["doc_type", "reason"],  # reason: oversize
)

document_parse_mode_total = Counter(
    "astrolabe_document_parse_mode_total",
    "Structured-tier parses by extraction mode",
    ["mode"],  # markdown | text_only
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
    ["reason"],  # reason: timeout | oom | error | unreadable
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
    "Documents dead-lettered by a terminal parse failure (no escalation tier) "
    "or by repeated index failures",
    # reason: a parse-failure reason (timeout | oom | error | oversize |
    # unreadable | unsupported_type) OR a vector.processor._drop_reason value
    # (timeout | connection | rate_limit | server | qdrant | other) for the
    # repeated-index-failure path. Keep in step with record_document_dead_lettered's
    # docstring below and with _drop_reason.
    ["reason"],
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

# File deletions the scanner declined to enqueue because a tag-discovery cycle
# returned zero files for an index mode while Qdrant still held indexed points
# for it — treated as a flaky/empty read, not "all files gone" (see
# _plan_file_deletions in vector/scanner.py). A rising rate here means the
# tenant's Nextcloud is answering the systemtag REPORT with intermittent empties;
# each increment is a deletion that was correctly withheld.
vector_sync_deletions_suppressed_total = Counter(
    "astrolabe_vector_sync_deletions_suppressed_total",
    "File deletions suppressed because tag discovery returned an implausible "
    "empty result (treated as a failed read), by index mode",
    ["index_mode"],  # hybrid | keyword
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

# Dense-vector RAM added per unit time, from OUR deterministic estimate at ingest
# (hybrid docs only; keyword docs embed no dense vector). The cumulative counter
# pairs with the corpus gauge above: rate() shows RAM-growth pressure, the gauge
# shows the live footprint. See ``record_estimated_vector_bytes``.
estimated_vector_bytes_total = Counter(
    "astrolabe_estimated_vector_bytes_total",
    "Estimated dense-vector RAM added at ingest (chunks * dim * 4 * hnsw_overhead)",
    ["doc_type"],
)

# Chunk density = chunks per MB of source content, observed once per embedded
# document. This is the "density risk" distribution from the cost-to-serve note:
# born-digital text sits around ~91 chunks/MB, while dense/low-fill docs push the
# tail higher and disproportionately inflate vector RAM relative to the billed
# source bytes. Buckets straddle that band so the risky tail is visible. Recorded
# for both index modes (density is a property of content, not of dense-vs-keyword).
#
# Shared with the current-corpus snapshot GaugeHistogram
# (``astrolabe_qdrant_chunk_density_chunks_per_mb_current``) so the ingest-flow
# panel and the current-distribution panel use identical bucket edges and are
# directly comparable.
CHUNK_DENSITY_BUCKETS = (1, 5, 10, 20, 40, 60, 91, 120, 160, 200, 300, 500)

document_chunk_density_chunks_per_mb = Histogram(
    "astrolabe_document_chunk_density_chunks_per_mb",
    "Chunks produced per MB of source content, per embedded document",
    ["doc_type"],
    buckets=CHUNK_DENSITY_BUCKETS,
)

# -----------------------------------------------------------------------------
# Current-corpus chunk-density snapshot (GaugeHistogram).
#
# Unlike the ingest-time Histogram above — which accumulates one observation per
# document as it is embedded and never decrements — this is a *snapshot* of the
# density distribution of the documents CURRENTLY resident in Qdrant, recomputed
# periodically by scrolling the collection (see
# ``vector.metrics_publisher.vector_density_snapshot_task``). A GaugeHistogram is
# the correct Prometheus type: its buckets rise and fall as the corpus changes.
#
# Fed forward-only: only documents whose Qdrant payload carries
# ``payload_keys.SOURCE_BYTES`` contribute. Documents indexed before that key
# shipped (or otherwise missing a usable source size) are counted separately in
# ``chunk_density_uncovered_documents`` so the snapshot's coverage is explicit.
# -----------------------------------------------------------------------------
QDRANT_CHUNK_DENSITY_CURRENT_METRIC = (
    "astrolabe_qdrant_chunk_density_chunks_per_mb_current"
)


class _ChunkDensitySnapshotCollector(Collector):
    """Custom collector exposing the current-corpus density as a GaugeHistogram.

    Holds the most recent snapshot, keyed by ``doc_type``. ``update`` replaces the
    whole snapshot atomically (a fresh scroll produces a complete new picture);
    ``collect`` yields one GaugeHistogram sample set per ``doc_type``. Emits
    nothing until the first snapshot lands, so a scrape before the publisher's
    first pass simply omits the metric rather than reporting a misleading zero.

    ``_snapshot`` maps ``doc_type -> (cumulative_buckets, gsum)`` where
    ``cumulative_buckets`` is a list of ``(le_str, cumulative_count)`` including
    the terminal ``"+Inf"`` bucket, matching Prometheus cumulative-bucket
    semantics. ``gcount`` is the ``+Inf`` count, so it is not stored separately.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: dict[str, tuple[list[tuple[str, float]], float]] = {}

    def update(
        self, snapshot: dict[str, tuple[list[tuple[str, float]], float]]
    ) -> None:
        with self._lock:
            self._snapshot = snapshot

    def collect(self):
        with self._lock:
            snapshot = self._snapshot
        if not snapshot:
            return
        family = GaugeHistogramMetricFamily(
            QDRANT_CHUNK_DENSITY_CURRENT_METRIC,
            "Chunks per MB of source content across documents currently in Qdrant "
            "(snapshot, recomputed periodically)",
            labels=["doc_type"],
        )
        for doc_type, (buckets, gsum) in snapshot.items():
            family.add_metric([doc_type], buckets, gsum_value=gsum)
        yield family


chunk_density_snapshot_collector = _ChunkDensitySnapshotCollector()
REGISTRY.register(chunk_density_snapshot_collector)

# Documents currently in Qdrant that could NOT be placed in the density snapshot
# because they carry no usable source-byte size (payload predates
# payload_keys.SOURCE_BYTES, or the value is missing/non-positive). Makes the
# forward-only coverage gap visible instead of silently shrinking the histogram.
chunk_density_uncovered_documents = Gauge(
    "astrolabe_qdrant_chunk_density_uncovered_documents",
    "Documents in Qdrant excluded from the chunk-density snapshot (no source_bytes)",
    ["doc_type"],
)

# 1 when the last density snapshot stopped early at the scan cap
# (vector_density_snapshot_max_documents) and so covers only a prefix of the
# collection; 0 when the whole collection was scanned. Alertable so a truncated
# snapshot is never mistaken for a complete one.
chunk_density_snapshot_truncated = Gauge(
    "astrolabe_qdrant_chunk_density_snapshot_truncated",
    "1 if the last chunk-density snapshot hit the document scan cap (partial)",
)

# Sum of ``source_bytes`` across documents currently resident in Qdrant, per
# ``doc_type``. Covers exactly the same forward-only set as the density snapshot
# histogram (documents without a usable source_bytes are excluded and surfaced via
# ``chunk_density_uncovered_documents``). Enables a corpus-weighted (byte-weighted)
# chunk density in Grafana — ``sum(indexed_chunks) / (sum(source_bytes) / 1e6)`` —
# which is the pricing driver, vs the doc-weighted mean of the histogram. This is a
# Gauge, not a Counter, because it falls when documents are deleted from the
# collection; the ``_total`` name is fixed (the deployed dashboard query depends on
# it) and denotes the aggregate over the corpus, not a monotonic counter.
qdrant_source_bytes_total = Gauge(
    "astrolabe_qdrant_source_bytes_total",
    "Total source_bytes across documents currently resident in Qdrant "
    "(byte-weighted density denominator; forward-only — docs without source_bytes "
    "are excluded, see astrolabe_qdrant_chunk_density_uncovered_documents)",
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

document_download_truncated_total = Counter(
    "astrolabe_document_download_truncated_total",
    "Times a WebDAV GET returned fewer bytes than Content-Length (truncated/"
    "poisoned connection; raised as a retryable transport error, see #965)",
)

# =============================================================================
# Astrolabe Search Metrics
# =============================================================================
#
# Semantic search has TWO entrypoints — the ``nc_semantic_search`` MCP tool and
# ``POST /api/v1/search`` (what Astrolabe calls) — and before these existed each
# was visible only through its transport's generic counters
# (``mcp_tool_calls_total`` / ``mcp_http_requests_total``). Neither reported what
# search actually did: how many results came back, how many the ACL check
# dropped, which granularity ran, or where the time went.
#
# Every metric here carries ``surface`` so the two entrypoints are directly
# comparable on one dashboard, and all of them are recorded from
# ``record_search_request`` / ``record_search_stage`` below so the surfaces
# cannot drift apart as one of them gains a feature.

search_requests_total = Counter(
    "astrolabe_search_requests_total",
    "Total semantic searches, by entrypoint and configuration",
    # surface: mcp | http — algorithm: bm25_hybrid_rrf | bm25_hybrid_dbsf | semantic
    # granularity: chunk | document — reranked: true | false | unavailable
    # status: success | error
    ["surface", "algorithm", "granularity", "reranked", "status"],
)

# Per-stage latency, so a slow search can be attributed without a trace.
# stage: retrieve (embed + Qdrant) | rerank (cross-encoder) | verify (verify-on-read).
# Buckets run to 30s because rerank over a deep pool is seconds, not milliseconds,
# and verify-on-read is bounded by Nextcloud round-trips rather than by us.
search_stage_duration_seconds = Histogram(
    "astrolabe_search_stage_duration_seconds",
    "Semantic search stage duration in seconds",
    ["surface", "stage"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# Results actually returned to the caller (post-verification, post-trim). A
# distribution that collapses toward zero is the signal that the corpus, the
# filters, or verify-on-read is starving callers — invisible in a request count.
search_results_returned = Histogram(
    "astrolabe_search_results_returned",
    "Results returned to the caller per search",
    ["surface"],
    buckets=(0, 1, 2, 5, 10, 20, 50, 100),
)

# Documents dropped by verify-on-read (ADR-019 ghost records). Sustained
# non-zero means the index is drifting from Nextcloud faster than webhooks
# reconcile it, which degrades recall silently because the over-fetch absorbs it.
search_verification_dropped_total = Counter(
    "astrolabe_search_verification_dropped_total",
    "Documents dropped by verify-on-read during search",
    ["surface"],
)

# Documents scored by the cross-encoder. This is the honest cost unit for
# reranking — there is no natural token unit — and the series to correlate
# against the gateway's own saturation signals when reranking gets slow. How
# that service is deployed is not something this server knows or should encode.
search_rerank_documents_total = Counter(
    "astrolabe_search_rerank_documents_total",
    "Documents scored by the reranker",
    ["model", "outcome"],  # outcome: success | degraded
)


# =============================================================================
# Database Metrics
# =============================================================================

db_operations_total = Counter(
    "mcp_db_operations_total",
    "Total database operations",
    # db: sqlite | postgresql | qdrant; operation varies
    ["db", "operation", "status"],
)

# Buckets deliberately reach 10s. The original ceiling was 1.0s, which made this
# histogram blind to the only incident it ever needed to catch: a per-operation
# regression from 65ms to ~1.9s (Deck #678) put ~50% of Postgres samples in the
# +Inf bucket, so p95/p99 were unusable and the regression was visible only in
# Tempo. Every original edge is retained so historical series stay comparable.
# The 0.75/1.5 edges straddle a 1s TCP RTO on purpose: a network-retry stall
# piles up against them, which distinguishes it from genuinely slow DB work.
db_operation_duration_seconds = Histogram(
    "mcp_db_operation_duration_seconds",
    "Database operation duration in seconds (includes connection acquisition)",
    ["db", "operation"],
    buckets=(
        0.001,
        0.0025,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        0.75,
        1.0,
        1.5,
        2.5,
        5.0,
        10.0,
    ),
)

# Connection acquisition, split out from the operation it serves. Under NullPool
# (ADR-026) every operation opens a fresh connection, so this is a real per-op
# cost, not a startup cost — and `db_operation_duration_seconds` above bundles it
# with execute, which is exactly why a ~600ms connect hid inside a "slow insert"
# for a day (Deck #678: one PgBouncer replica was scheduled in another
# continent; ~50% of connections paid a transatlantic TLS handshake).
# Labelled by dialect only: an "operation" is meaningless for a connect.
db_connect_duration_seconds = Histogram(
    "mcp_db_connect_duration_seconds",
    "Database connection acquisition duration in seconds",
    ["db"],
    buckets=(
        0.001,
        0.0025,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        0.75,
        1.0,
        1.5,
        2.5,
        5.0,
        10.0,
    ),
)

# pypdfium2 / pymupdf are not thread-safe; concurrent ingest jobs serialize their
# native calls on per-library locks (see document_processors/_native_locks.py).
# This surfaces the resulting contention so per-tier `concurrency` can be tuned.
pdf_native_lock_wait_seconds = Histogram(
    "astrolabe_pdf_native_lock_wait_seconds",
    "Time spent waiting to acquire a native PDF library lock",
    ["library"],  # library: pdfium | pymupdf
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)


def record_pdf_native_lock_wait(library: str, seconds: float) -> None:
    """Record the wait to acquire the PDFium/MuPDF serialization lock."""
    pdf_native_lock_wait_seconds.labels(library=library).observe(seconds)


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


# =============================================================================
# MCP Client Fleet Instrumentation
# =============================================================================

_FLEET_CAPABILITIES = ("elicitation", "sampling", "roots")
_SESSION_RECORDED_ATTR = "_astrolabe_fleet_recorded"

# Ceiling on distinct client identities tracked. Truncating a label bounds how
# big one value can get; it does nothing about how *many* distinct values a peer
# can mint. `clientInfo.name` is chosen by the caller, so a client embedding a
# session id or random suffix would grow the series count without limit, and
# prometheus_client entries never expire. Beyond this many, everything new
# collapses into `_OVERFLOW_LABEL`.
#
# 50 is far above the real fleet (single digits) and far below anything that
# strains the registry, so crossing it is itself a signal — see the
# client_name="_other" alert in docs/observability.md.
# Causes already reported by _warn_once. Instrumentation that fails silently is
# the failure this whole metric family exists to remove, so its own failures say
# so — but once per process, not once per request, since these fire on a hot path.
_warned_causes: set[str] = set()

_MAX_TRACKED_CLIENTS = 50
_OVERFLOW_LABEL = "_other"
_seen_client_names: set[str] = set()
# Separate registry for OAuth client_ids: they come off *unverified* tokens on
# the rejection path, so they are at least as untrusted as clientInfo.name and
# must not share its budget.
_seen_client_ids: set[str] = set()


def _warn_once(cause: str, message: str, *args: object) -> None:
    """Log a cause the first time it occurs in this process, then stay quiet."""
    # ponytail: unsynchronised check-then-set, matching _bounded_label and the
    # session claim. Worst case is one duplicate line per cause per process, on
    # a first-request race; a lock on a logging gate would cost more.
    if cause in _warned_causes:
        return
    _warned_causes.add(cause)
    logger.warning(message, *args)


def _client_label(value: object, limit: int = 64) -> str:
    """Clamp a client-supplied value's *length* before it becomes a label.

    ``clientInfo`` arrives from the peer, so its length is not ours to trust:
    an unbounded label value is a memory exhaustion vector against the
    process-global registry.

    Note this bounds the size of one value, not the number of distinct values —
    see :func:`_bounded_client_name` for that half.

    Empty maps to "unknown" alongside None. `AccessToken.client_id` defaults to
    `""` when the payload carries no client_id claim, so without this an absent
    identity would record as an empty label — a second spelling of "we don't
    know" that splits the series and reads as a rendering bug on a dashboard.
    """
    return str(value)[:limit] if value else "unknown"


def _first_present(*candidates: tuple[object, str]) -> object | None:
    """Return the first attribute that is actually *present*, else None.

    Deliberately tests ``is not None`` rather than truthiness. The obvious
    ``getattr(a, "x", None) or getattr(b, "y", None)`` would treat a present
    but falsy value — an empty string, a 0 — as missing and silently fall
    through to the other spelling. That is not hypothetical for the SDK
    upgrade this instrumentation exists to watch: mcp 2.x has unversioned
    servers report an empty string rather than omitting the field.
    """
    for obj, attr in candidates:
        value = getattr(obj, attr, None)
        if value is not None:
            return value
    return None


def _bounded_label(value: str, seen: set[str]) -> str:
    """Bound the *number* of distinct values a peer can put in one label.

    Returns ``value`` while under the cap, ``_OVERFLOW_LABEL`` after. Paired
    with :func:`_client_label` this closes the cardinality vector from both
    ends: a peer can neither send one enormous value nor mint unboundedly many
    small ones.

    ``seen`` is per-dimension so one untrusted label cannot exhaust another's
    budget.
    """
    if value in seen:
        return value
    if len(seen) >= _MAX_TRACKED_CLIENTS:
        return _OVERFLOW_LABEL
    # ponytail: unsynchronised. Concurrent first-sightings can race past the
    # cap by the number of in-flight requests — irrelevant against a limit of
    # 50, and a lock on every request would cost more than the slack.
    seen.add(value)
    return value


def _bounded_client_name(name: str) -> str:
    """Bound the number of distinct MCP client identities (``clientInfo.name``)."""
    return _bounded_label(name, _seen_client_names)


def _minor_version(version: object) -> str:
    """Reduce a version to ``major.minor``.

    Full patch versions would add a new series on every client release, which
    buys nothing — we want to spot "Claude Code 2.1 changed protocol version",
    not track point releases.
    """
    return ".".join(_client_label(version, 32).split(".")[:2])


def record_client_session(ctx: ServerRequestContext | None) -> None:
    """Record the calling MCP client's identity and capabilities, once per session.

    Takes the SDK's ``ServerRequestContext`` (v2 removed the ``request_ctx``
    contextvar that v1 read instead), so it works in every deployment mode
    (single-user, multi-user BasicAuth, Login Flow v2, OAuth) and for every
    tool, including the auth/oauth tools that carry no ``@instrument_tool``.

    Never raises: instrumentation must not be able to fail a tool call.
    """
    if ctx is None:
        # Legitimate outside a request (startup, direct calls in tests), but this
        # function is only wired into the tools/call middleware, where a missing
        # request context means the wiring is wrong.
        _warn_once(
            "no_request_ctx",
            "MCP client fleet metrics: no request context on a tool call — "
            "mcp_client_sessions_total will stay empty",
        )
        return
    session = ctx.session
    # ponytail: unsynchronised check-then-set, so two requests racing on a
    # brand-new session can each record it once. Bounded at one extra count per
    # session; a lock here would cost more than the error is worth.
    if getattr(session, _SESSION_RECORDED_ATTR, False):
        return
    params = getattr(session, "client_params", None)
    if params is None:
        # Expected once, between transport creation and the initialize handshake
        # completing. Persisting past that means the session this handler sees is
        # not the one that processed initialize, and the fleet metric will never
        # populate — which is exactly what happened in 0.162.0.
        _warn_once(
            "no_client_params",
            "MCP client fleet metrics: session %s has no client_params on a tool "
            "call — mcp_client_sessions_total will stay empty",
            type(session).__name__,
        )
        return
    # Claim the session before doing the work, so a failure below costs one
    # warning for this session rather than one per tool call.
    setattr(session, _SESSION_RECORDED_ATTR, True)

    try:
        # --- the only SDK-version-sensitive lines in the codebase ------------
        # mcp 1.x: client_params.clientInfo / .capabilities / .protocolVersion
        # mcp 2.x: client_params.client_info, session.client_capabilities,
        #          request_context.protocol_version (negotiated, not requested).
        # Both spellings are tried so this keeps recording *through* the
        # upgrade. A metric that silently zeroed itself at the moment of
        # comparison would destroy the baseline it exists to provide.
        #
        # Note these getattr defaults swallow AttributeError, so a rename that
        # neither spelling covers does NOT raise — it records
        # client_name="unknown". That is deliberate (instrumentation must not
        # fail a tool call) and is the post-upgrade signal to alert on; see
        # docs/observability.md.
        info = _first_present((params, "client_info"), (params, "clientInfo"))
        caps = _first_present(
            (session, "client_capabilities"), (params, "capabilities")
        )
        protocol = _first_present(
            (ctx, "protocol_version"), (params, "protocolVersion")
        )
        # ---------------------------------------------------------------------
        name = _bounded_client_name(_client_label(getattr(info, "name", None)))
        mcp_client_sessions_total.labels(
            client_name=name,
            client_version=_minor_version(getattr(info, "version", None)),
            protocol_version=_client_label(protocol, 32),
        ).inc()
        for capability in _FLEET_CAPABILITIES:
            # Set explicitly to 0 when absent, so a client that *stops*
            # declaring a capability shows a drop rather than a stale 1.
            mcp_client_capability.labels(client_name=name, capability=capability).set(
                1 if getattr(caps, capability, None) is not None else 0
            )
    except Exception:
        # Backstop for anything the accessors above don't absorb (a label-value
        # rejection, say). WARNING rather than DEBUG because this metric is the
        # baseline for an SDK upgrade — losing it silently defeats the purpose.
        # Bounded to one line per session by the claim above.
        logger.warning(
            "Could not record MCP client session metrics — the SDK's client_params "
            "shape may have changed",
            exc_info=True,
        )


def record_elicitation(prompt: str, outcome: str, reason: str = "none") -> None:
    """Record the outcome of an elicitation prompt.

    Args:
        prompt: Which prompt ran — "login_flow" or "provisioning_required".
        outcome: "accepted", "declined", "cancelled", or "message_only". These
            are exactly the strings ``_run_elicit`` returns, so the metric and
            that function's contract cannot drift apart.
        reason: Why a message_only fallback happened — "no_elicit_attr" (the
            context exposes no elicit()), "not_implemented" (the client declined
            the capability), or "error" (elicit raised). "none" for the three
            real outcomes. mcp 2.x adds "no_back_channel".
    """
    mcp_elicitation_total.labels(prompt=prompt, outcome=outcome, reason=reason).inc()


# Redaction is by key *substring*, case-insensitively: the arguments reaching
# the CallToolRequest handler are unvalidated client input (MCPServer registers it
# with validate_input=False), so a caller can send "password" to a tool that
# declares no such parameter, and a tool that does take one may call it
# "access_token" or "clientSecret".
_SENSITIVE_ARG_SUBSTRINGS = (
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "cookie",
)

# Arguments that say nothing about how the tool is being used: the injected
# MCPServer Context and the concurrency etag every update tool carries.
_UNINTERESTING_ARGS = ("ctx", "etag")

# Per value first, so one huge argument (a file body, a note) cannot fill the
# whole budget and hide every other argument.
_MAX_ARG_VALUE_CHARS = 200
_MAX_ARGS_CHARS = 1000


def _sanitize_tool_args(arguments: Mapping[str, Any] | None) -> str | None:
    """Render tool arguments for a log line or a span attribute.

    Returns ``None`` when there is nothing worth recording, so callers omit the
    field entirely — OTel rejects None-valued attributes, logging a warning and
    dropping them.
    """
    if not arguments:
        return None

    parts = []
    for key, value in arguments.items():
        if key in _UNINTERESTING_ARGS:
            continue
        if any(s in key.lower() for s in _SENSITIVE_ARG_SUBSTRINGS):
            rendered = repr("[redacted]")
        else:
            rendered = repr(value)
            if len(rendered) > _MAX_ARG_VALUE_CHARS:
                rendered = rendered[:_MAX_ARG_VALUE_CHARS] + "…"
        parts.append(f"{key!r}: {rendered}")

    if not parts:
        return None
    return ("{" + ", ".join(parts) + "}")[:_MAX_ARGS_CHARS]


def _log_tool_call(
    requested_name: str,
    arguments: Mapping[str, Any] | None,
    tool_name: str,
    outcome: str,
    started: float,
) -> None:
    """Emit the one structured line per tool call that Loki queries.

    The spans from ``instrument_tool`` carry the same tool name and arguments,
    but traces are sampled and short-retention, so they cannot answer "which
    tools ran, with what inputs, over the last month". Logs are unsampled; that
    is the whole point of duplicating the information here.

    Field names are prefixed to stay clear of reserved ``LogRecord`` attributes
    (``name``, ``args``, ``message``), which ``extra=`` may not overwrite.
    """
    fields: dict[str, Any] = {
        "mcp_tool": tool_name,
        "outcome": outcome,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }

    if requested_name != tool_name:
        # Kept out of the metric label to bound cardinality, kept here because a
        # client sending an unregistered name is exactly what you want to see.
        fields["mcp_tool_requested"] = requested_name[:100]

    args = _sanitize_tool_args(arguments)
    if args:
        fields["mcp_tool_args"] = args

    # None in BasicAuth / single-user / stdio, where there is no OAuth identity.
    # Deliberately not extract_user_id_from_token(): that raises MCPError on a
    # token without a sub claim, which would turn logging into a tool failure.
    token = get_access_token()
    if token:
        if token.resource:
            fields["mcp_user"] = token.resource
        fields["mcp_client_id"] = token.client_id

    logger.log(
        logging.INFO if outcome == "success" else logging.WARNING,
        "tool call %s %s in %dms",
        tool_name,
        outcome,
        fields["duration_ms"],
        extra=fields,
    )


def instrument_call_tool_outcomes(mcp: MCPServer) -> None:
    """Append a middleware that records how the SDK replied to ``tools/call``.

    The tool_error/protocol_error distinction is made *inside* the SDK, one
    frame above ``MCPServer.call_tool``: the handler either returns
    ``CallToolResult(is_error=True)`` or lets an exception escape into the
    dispatcher, which turns it into a JSON-RPC error. A middleware is the only
    place that can observe which happened — mcp 2.x removed the
    ``request_handlers`` dict this used to patch.

    Doing the same classification inside ``instrument_tool`` by inspecting the
    exception type would merely re-encode our current belief about the SDK, so
    the metric would read identically before and after the 2.x upgrade and
    detect nothing — which is the one thing this metric exists to do.
    """

    def _tool_label(name: str) -> str:
        """Reduce a requested tool name to a registered one, or "unknown".

        ``req.params.name`` is whatever the caller put in the JSON-RPC body; it
        is not validated against the registry before this handler runs. Used
        raw it would be an unbounded-cardinality vector — a client sending
        garbage names mints a permanent series each — and it would pollute the
        ``protocol_error`` tripwire with caller-chosen values that have nothing
        to do with the SDK upgrade this metric exists to watch.

        Resolved per request rather than snapshotted, so tools registered after
        this wrapper is installed still label correctly.
        """
        try:
            if mcp._tool_manager.get_tool(name) is not None:
                return name
        except Exception:  # pragma: no cover - registry shape is SDK-internal
            logger.debug("Could not consult the tool registry", exc_info=True)
        return "unknown"

    async def middleware(ctx: ServerRequestContext, call_next: CallNext) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        record_client_session(ctx)
        params: Mapping[str, Any] = ctx.params or {}
        requested = params.get("name")
        requested = requested if isinstance(requested, str) else ""
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, Mapping) else None
        tool_name = _tool_label(requested)
        started = time.perf_counter()
        try:
            result = await call_next(ctx)
        except Exception:
            # Deliberately Exception, not BaseException: anyio cancellation is
            # not a protocol error and must not poison this signal.
            mcp_tool_outcomes_total.labels(
                tool_name=tool_name, outcome="protocol_error"
            ).inc()
            _log_tool_call(requested, arguments, tool_name, "protocol_error", started)
            raise
        outcome = "tool_error" if getattr(result, "is_error", False) else "success"
        mcp_tool_outcomes_total.labels(tool_name=tool_name, outcome=outcome).inc()
        _log_tool_call(requested, arguments, tool_name, outcome, started)
        return result

    mcp.middleware.append(middleware)


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
        reason: Retry reason (429, 423, timeout, connection_error)
    """
    nextcloud_api_retries_total.labels(app=app, reason=reason).inc()


def record_oauth_token_validation(
    method: str,
    result: str,
    reason: str = "none",
    client_id: str | None = None,
) -> None:
    """
    Record an OAuth token validation.

    The permitted values for ``method``, ``result`` and ``reason`` are
    enumerated once, on the ``oauth_token_validations_total`` definition above.
    This docstring deliberately does not repeat them: it used to, drifted out of
    sync within a single PR, and made a third copy of a list that already exists
    in two places. Describing what each argument *means* is this docstring's
    job; the vocabulary has one home.

    Args:
        method: Which validator produced this outcome.
        result: Whose problem it is — valid, the caller's token, or ours.
            Derived from ``reason`` by ``UnifiedTokenVerifier._reject``; never
            pass it independently of the reason.
        reason: Why a non-valid result happened. "none" for a valid result.
        client_id: OAuth client the token belongs to. Verified on the
            authorization paths, but read from an *unverified* token on the
            validation ones — so treated as untrusted regardless, and both
            length-clamped and count-bounded.
    """
    oauth_token_validations_total.labels(
        method=method,
        result=result,
        reason=reason,
        client_id=_bounded_label(_client_label(client_id), _seen_client_ids),
    ).inc()


def record_oauth_grant(
    grant_type: str, result: str, refresh_token: str = "unknown"
) -> None:
    """Record a grant processed by the AS proxy token endpoint.

    The label vocabulary is enumerated once, on the ``oauth_grants_total``
    definition above; this docstring does not repeat it.

    ``client_id`` is deliberately NOT a label here. It would be the natural
    fourth dimension, but the grant counters are already multiplied by
    grant_type x result x refresh_token, and the per-client attribution is
    available on ``oauth_token_validations_total``. The log line emitted
    alongside each of these carries the client_id when it is needed.

    Args:
        grant_type: Which grant the client asked for.
        result: Whether the AS proxy could satisfy it.
        refresh_token: Whether the resulting token response carried a refresh
            token. "unknown" when the grant failed and there was no response
            to inspect.
    """
    oauth_grants_total.labels(
        grant_type=grant_type, result=result, refresh_token=refresh_token
    ).inc()


def record_db_operation(
    db: str, operation: str, duration: float, status: str = "success"
) -> None:
    """
    Record a database operation.

    Note that ``duration`` covers connection acquisition as well as execution;
    use :func:`record_db_connect` to attribute the acquire half.

    Args:
        db: Database type ("sqlite", "postgresql", or "qdrant")
        operation: Operation type (e.g., "insert", "select", "upsert", "search")
        duration: Operation duration in seconds
        status: "success" or "error"
    """
    db_operations_total.labels(db=db, operation=operation, status=status).inc()
    db_operation_duration_seconds.labels(db=db, operation=operation).observe(duration)


def record_db_connect(db: str, duration: float) -> None:
    """
    Record a database connection acquisition.

    Under NullPool this fires once per operation, so a rising ``_count`` rate is
    normal. If connection pooling is ever reintroduced, that rate collapsing
    toward zero is the signal the pool is actually being reused.

    Args:
        db: Database type ("sqlite" or "postgresql")
        duration: Time to acquire the connection, in seconds
    """
    db_connect_duration_seconds.labels(db=db).observe(duration)


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


def record_vector_sync_deletions_suppressed(index_mode: str, count: int) -> None:
    """
    Record file deletions withheld this scan because tag discovery for an index
    mode came back empty while Qdrant still held indexed points (a suspected
    flaky read rather than a genuine mass-untag).

    Args:
        index_mode: The index mode whose deletions were suppressed (hybrid|keyword)
        count: Number of would-be deletions withheld this cycle
    """
    vector_sync_deletions_suppressed_total.labels(index_mode=index_mode).inc(count)


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


def update_vector_sync_dead_lettered_documents(count: int) -> None:
    """Set the dead-lettered-documents gauge."""
    vector_sync_dead_lettered_documents.set(count)


def update_vector_sync_estimated_vector_bytes(byte_estimate: float) -> None:
    """Set the deterministic dense-vector RAM-footprint gauge (from hybrid chunks)."""
    vector_sync_estimated_vector_bytes.set(byte_estimate)


def update_vector_sync_qdrant_vectors(count: int) -> None:
    """Set the Qdrant-reported dense-vector count gauge."""
    vector_sync_qdrant_vectors.set(count)


def update_vector_sync_qdrant_vector_bytes(byte_estimate: float) -> None:
    """Set the dense-vector RAM-footprint gauge derived from Qdrant vectors_count."""
    vector_sync_qdrant_vector_bytes.set(byte_estimate)


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
        LEGACY_OCR_QUEUES,
    )

    for queue in (
        *ALL_INGEST_QUEUES,
        LEGACY_INGEST_QUEUE,
        *sorted(LEGACY_OCR_QUEUES),
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
        status: "success" | "error" | "pending" (a batch-OCR poll still in flight —
            GPU booting / batch queued; re-queued via BatchPending, not a failure)
    """
    document_parse_duration_seconds.labels(
        processor=processor, tier=tier, status=status
    ).observe(duration)
    document_parse_total.labels(processor=processor, tier=tier, status=status).inc()
    # Throughput counters (pages/chars/bytes) accrue only on a full success.
    # A partial extraction flagged success=False (recorded above as "error") or a
    # batch-OCR poll still in flight ("pending") is intentionally excluded here so
    # low-confidence output and GPU-boot polling never inflate pipeline throughput.
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
        reason: ``timeout`` | ``oom`` | ``error`` (from the isolated worker);
            ``unreadable`` -- the engine could not open the bytes as a document
            at all, i.e. the file's content does not match the mime type its
            extension claimed. Distinguished from ``error`` on purpose: it means
            corrupt input, not a failure of ours, so it should not page anyone;
            ``oversize`` -- rejected by the pre-parse size guard, no tier can
            ever parse it; or ``unsupported_type`` -- no registered processor
            claims the mime type, which is a property of what this deployment
            has enabled rather than of the document. Kept in step with
            ``record_document_dead_lettered`` below: the terminal ones are
            counted on both.
    """
    document_parse_failed_total.labels(reason=reason).inc()


def record_document_ingest_size(doc_type: str, size_bytes: int) -> None:
    """Record the source size of a document offered for ingest.

    Called before the oversize gate so the distribution includes documents that
    are subsequently rejected -- they are exactly the ones that drive cap, spool
    and memory sizing. A size of 0/None means the source did not report one, and
    is skipped rather than recorded as a real zero, which would pile a false
    spike into the smallest bucket.
    """
    if size_bytes > 0:
        document_ingest_size_bytes.labels(doc_type=doc_type).observe(size_bytes)


def record_document_ingest_rejected(doc_type: str, reason: str) -> None:
    """Record a document rejected before parsing (currently ``oversize``).

    Paired with :func:`record_document_ingest_size` so "what fraction of this
    tenant's corpus is over cap" is a ratio of two metrics rather than an
    investigation.
    """
    document_ingest_rejected_total.labels(doc_type=doc_type, reason=reason).inc()


def record_document_parse_mode(mode: str) -> None:
    """Record which extraction mode the structured tier used.

    Deliberately NOT folded into ``document_parse_failed_total``: skipping
    markdown is a successful parse, not a failure. Without its own signal the
    page gate (``document_markdown_max_pages``) is invisible -- there is no way
    to tell "markdown is off for most of this tenant" from "markdown is running
    fine", which is exactly the question an operator asks after tuning it.
    """
    document_parse_mode_total.labels(mode=mode).inc()


def record_document_dead_lettered(reason: str) -> None:
    """Record a document dead-lettered, by parse failure or repeated index failure.

    Counts the dead-letter *attempt*: it is incremented alongside the
    ``mark_dead_letter`` call, which is fail-safe (a Qdrant write error is logged,
    not raised), so a transient write failure can leave this counter marginally
    above the live marker count in Qdrant.

    Args:
        reason: for a **terminal parse failure** — ``timeout`` | ``oom`` |
            ``error`` (the reason carried from the isolated worker),
            ``oversize`` (rejected by the pre-parse size guard, which no tier can
            ever parse), ``unreadable`` (the bytes are not a document the engine
            can open -- no tier will do better, so it is terminal on the first
            attempt), or ``unsupported_type`` (no registered processor claims the
            mime type -- a config property of the deployment, not a fault of the
            document).

            For a document parked after ``VECTOR_SYNC_MAX_INDEX_FAILURES``
            consecutive **index** failures (GH #1345), the label is instead a
            ``vector.processor._drop_reason`` value: ``timeout`` |
            ``connection`` | ``rate_limit`` | ``server`` | ``qdrant`` |
            ``other``. ``timeout`` is therefore shared by both paths. The label
            set is not validated anywhere, so this list is the only contract --
            keep it in step with ``_drop_reason``.
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


def record_search_request(
    *,
    surface: str,
    algorithm: str,
    granularity: str,
    reranked: str,
    status: str,
    results_returned: int | None = None,
    verification_dropped: int = 0,
) -> None:
    """Record one semantic search from either entrypoint.

    Both ``nc_semantic_search`` (surface ``"mcp"``) and ``POST /api/v1/search``
    (surface ``"http"``) call this, so the two are comparable on one dashboard
    and cannot drift as either gains a feature.

    Args:
        surface: ``"mcp"`` or ``"http"``.
        algorithm: Search method label, e.g. ``bm25_hybrid_rrf`` or ``semantic``.
        granularity: ``chunk`` or ``document``.
        reranked: ``"true"`` when the reranker ordered the results,
            ``"false"`` when it was not requested, ``"unavailable"`` when it was
            requested but degraded to retrieval order. Kept as a string label
            because those are three distinct states, not a boolean.
        status: ``success`` or ``error``.
        results_returned: Results handed to the caller. ``None`` on the error
            path, where the count is not meaningful.
        verification_dropped: Documents removed by verify-on-read.
    """
    search_requests_total.labels(
        surface=surface,
        algorithm=algorithm,
        granularity=granularity,
        reranked=reranked,
        status=status,
    ).inc()
    if results_returned is not None:
        search_results_returned.labels(surface=surface).observe(results_returned)
    if verification_dropped > 0:
        search_verification_dropped_total.labels(surface=surface).inc(
            verification_dropped
        )


def record_search_stage(surface: str, stage: str, seconds: float) -> None:
    """Record the duration of one search stage (retrieve | rerank | verify)."""
    if seconds >= 0:
        search_stage_duration_seconds.labels(surface=surface, stage=stage).observe(
            seconds
        )


def record_rerank_documents(model: str, count: int, outcome: str) -> None:
    """Record documents scored by the reranker.

    Documents-scored is the honest cost unit: reranking has no natural token
    unit, and document count is what drives the work a rerank request asks of
    the gateway.
    ``outcome`` is ``"success"`` or ``"degraded"`` — the degraded count records
    what the reranker *would* have scored, so a reranker outage is visible as a
    shift between outcomes rather than as a silent gap in the series.
    """
    if count > 0:
        search_rerank_documents_total.labels(model=model, outcome=outcome).inc(count)


def record_document_chunks(doc_type: str, count: int) -> None:
    """
    Record the number of chunks produced for a document.

    Args:
        doc_type: Document source type (note, file, deck_card, news_item)
        count: Number of chunks produced
    """
    document_chunks_total.labels(doc_type=doc_type).inc(count)


# float32 elements — the dense-vector storage width. A module constant (not config)
# because it is a property of the vector encoding, not a deployment knob.
DENSE_VECTOR_BYTES_PER_DIMENSION = 4


def estimate_vector_bytes(chunk_count: int, dimension: int, overhead: float) -> float:
    """Estimate the dense-vector RAM footprint of ``chunk_count`` vectors.

    ``chunk_count * dimension * 4 (float32) * overhead``, where ``overhead`` is the
    HNSW-graph/segment multiplier (``VECTOR_RAM_HNSW_OVERHEAD_FACTOR``). Returns 0
    for a non-positive chunk count or dimension — a keyword-only document embeds no
    dense vector, so it must contribute nothing to the estimate.
    """
    if chunk_count <= 0 or dimension <= 0:
        return 0.0
    return chunk_count * dimension * DENSE_VECTOR_BYTES_PER_DIMENSION * overhead


def record_estimated_vector_bytes(doc_type: str, byte_estimate: float) -> None:
    """Record the estimated dense-vector RAM a document added at ingest.

    No-op when ``byte_estimate <= 0`` (keyword-only docs, empty docs) so the
    counter never advances on a document that stored no dense vector.
    """
    if byte_estimate > 0:
        estimated_vector_bytes_total.labels(doc_type=doc_type).inc(byte_estimate)


def record_chunk_density(doc_type: str, chunk_count: int, source_bytes: int) -> None:
    """Observe chunk density (chunks per MB of source) for one embedded document.

    Surfaces the "density risk" distribution: dense/low-fill docs produce many
    chunks per source byte and so inflate vector RAM relative to the billed source
    bytes. No-op when ``source_bytes <= 0`` (avoids a divide-by-zero) or when the
    document produced no chunks.
    """
    if source_bytes > 0 and chunk_count > 0:
        chunks_per_mb = chunk_count / (source_bytes / 1_000_000)
        document_chunk_density_chunks_per_mb.labels(doc_type=doc_type).observe(
            chunks_per_mb
        )


def density_bucket_index(chunks_per_mb: float) -> int:
    """Index into a per-bucket tally (``CHUNK_DENSITY_BUCKETS`` + overflow slot).

    Returns the position of the first bucket whose upper edge is ``>=`` the value,
    or ``len(CHUNK_DENSITY_BUCKETS)`` (the trailing ``"+Inf"`` overflow slot) when
    the value exceeds every finite edge. The companion tally therefore has length
    ``len(CHUNK_DENSITY_BUCKETS) + 1``. Shared by the snapshot publisher so its
    bucketing matches these exact edges.
    """
    for idx, edge in enumerate(CHUNK_DENSITY_BUCKETS):
        if chunks_per_mb <= edge:
            return idx
    return len(CHUNK_DENSITY_BUCKETS)


def update_qdrant_chunk_density_snapshot(
    per_doc_type: dict[str, tuple[list[float], float]],
    *,
    uncovered: dict[str, int] | None = None,
    truncated: bool = False,
    source_bytes: dict[str, float] | None = None,
) -> None:
    """Publish one current-corpus chunk-density snapshot (GaugeHistogram + coverage).

    ``per_doc_type`` maps ``doc_type -> (bucket_counts, gsum)`` where
    ``bucket_counts`` is a NON-cumulative per-bucket tally aligned to
    ``CHUNK_DENSITY_BUCKETS`` with one trailing overflow (``"+Inf"``) slot
    (length ``len(CHUNK_DENSITY_BUCKETS) + 1``, as produced via
    ``density_bucket_index``), and ``gsum`` is the sum of observed densities. The
    tally is converted to Prometheus cumulative ``(le, count)`` buckets here — the
    single place cumulative-bucket semantics live — and the GaugeHistogram
    snapshot is swapped atomically.

    ``uncovered`` (doc_type -> count of docs with no usable source size) and
    ``truncated`` (scan hit the document cap) update the companion coverage
    gauges. The uncovered gauge is fully reset each snapshot so a doc_type that
    falls back to zero uncovered does not leave a stale series.

    ``source_bytes`` (doc_type -> sum of source_bytes over the covered documents)
    updates ``astrolabe_qdrant_source_bytes_total``, the byte-weighted density
    denominator. It is reset-then-repopulated the same way as the uncovered gauge.
    """
    edges = [str(b) for b in CHUNK_DENSITY_BUCKETS] + ["+Inf"]
    snapshot: dict[str, tuple[list[tuple[str, float]], float]] = {}
    for doc_type, (bucket_counts, gsum) in per_doc_type.items():
        cumulative: list[tuple[str, float]] = []
        running = 0.0
        for le, count in zip(edges, bucket_counts):
            running += count
            cumulative.append((le, running))
        snapshot[doc_type] = (cumulative, gsum)
    chunk_density_snapshot_collector.update(snapshot)

    # Reset then repopulate so a doc_type absent this round drops to no series.
    chunk_density_uncovered_documents.clear()
    for doc_type, count in (uncovered or {}).items():
        chunk_density_uncovered_documents.labels(doc_type=doc_type).set(count)

    # Same reset-then-repopulate: a doc_type that leaves the corpus clears its series.
    qdrant_source_bytes_total.clear()
    for doc_type, total in (source_bytes or {}).items():
        qdrant_source_bytes_total.labels(doc_type=doc_type).set(total)

    chunk_density_snapshot_truncated.set(1 if truncated else 0)


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

        # kwargs is post-validation, so it carries the tool's defaults and the
        # injected ctx — unlike the raw wire arguments _log_tool_call renders.
        attributes: dict[str, Any] = {"mcp.tool.name": tool_name}
        tool_args = _sanitize_tool_args(kwargs)
        if tool_args:
            attributes["mcp.tool.args"] = tool_args

        # Create trace span with metrics collection
        with trace_operation(
            f"mcp.tool.{tool_name}",
            attributes=attributes,
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
