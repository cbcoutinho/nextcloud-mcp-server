"""Optional cross-encoder rerank stage, shared by both search entrypoints.

Sits ABOVE the algorithm layer — after retrieval and merge, before
verify-on-read — so it applies identically to ``nc_semantic_search`` and
``POST /api/v1/search``, and to every search algorithm, without either surface
or any algorithm knowing about it.

Why before verification: reranking after it would mean verifying the whole deep
pool, i.e. one Nextcloud round-trip per candidate against a bounded semaphore,
which costs more than the reranker. Candidates are already ACL-filtered inside
Qdrant (``build_base_filter_conditions``); verify-on-read is a staleness check
on top. The consequence to know about is that a ghost record can occupy a rerank
slot and then be dropped, shortening the page — the same trade the existing
over-fetch already makes, with more headroom.

Reranking NEVER fails a search. Every failure path returns the input order.
"""

import logging
from typing import Any

import anyio

from nextcloud_mcp_server.observability.metrics import (
    record_rerank_documents,
    record_search_stage,
)
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.providers.gateway import build_gateway_token_provider
from nextcloud_mcp_server.providers.gateway_rerank import (
    GatewayRerankClient,
    RerankError,
)
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.bm25_hybrid import (
    DOCUMENT_PREFETCH_FACTOR,
    MAX_DOCUMENT_PREFETCH,
)

logger = logging.getLogger(__name__)

# Skip window after a failure. Without it every search in an outage pays the
# full rerank timeout before degrading, turning a reranker problem into a
# latency floor across the whole surface.
_FAILURE_COOLDOWN_SECONDS = 30.0

_client: GatewayRerankClient | None = None
_client_lock: anyio.Lock | None = None
_limiter: anyio.CapacityLimiter | None = None
_cooldown_until: float = 0.0


def _reset_rerank_state() -> None:
    """Drop cached client/limiter/cooldown. Test hook — mirrors the OCR
    backend's ``_reset_poll_batch_client``."""
    global _client, _client_lock, _limiter, _cooldown_until
    _client = None
    _client_lock = None
    _limiter = None
    _cooldown_until = 0.0


def rerank_available(settings: Any) -> bool:
    """Whether reranking can run at all on this deployment.

    The capability gate the request parameter is checked against, and what
    ``/api/v1/status`` advertises — so a caller can discover the feature instead
    of probing it and eating an error.
    """
    return bool(
        getattr(settings, "search_rerank_enabled", False)
        and getattr(settings, "embedding_gateway_url", None)
    )


async def _get_client(settings: Any) -> GatewayRerankClient | None:
    """Build (once) the shared rerank client. ``None`` when unavailable."""
    global _client, _client_lock
    if not rerank_available(settings):
        return None
    if _client is not None:
        return _client
    if _client_lock is None:
        _client_lock = anyio.Lock()
    async with _client_lock:
        if _client is None:
            _client = GatewayRerankClient(
                base_url=settings.embedding_gateway_url,
                model=settings.search_rerank_model,
                token_provider=build_gateway_token_provider(settings),
                timeout_seconds=float(settings.search_rerank_timeout_seconds),
            )
    return _client


def _get_limiter(settings: Any) -> anyio.CapacityLimiter:
    """Bound concurrent rerank calls.

    This does NOT protect a co-located embedding model's throughput — where the
    two share a device, one in-flight rerank is already enough to slow embedding
    substantially. What it bounds is queue depth and therefore rerank latency,
    so a burst of searches cannot stack unbounded work on the model.
    """
    global _limiter
    if _limiter is None:
        _limiter = anyio.CapacityLimiter(
            max(1, int(getattr(settings, "search_rerank_max_concurrency", 1)))
        )
    return _limiter


def effective_pool_size(settings: Any, *, floor: int, grouped: bool) -> int:
    """Candidate depth to retrieve when reranking.

    Args:
        settings: Live settings.
        floor: The over-fetch the surface would use anyway. The pool never goes
            BELOW it, or a large ``limit`` would retrieve fewer candidates with
            reranking on than off.
        grouped: Whether the request uses document granularity.

    Grouped search is capped: the grouped prefetch is bounded by
    ``MAX_DOCUMENT_PREFETCH``, and requesting more groups than that prefetch can
    fill makes Qdrant widen its grouping search and reorder the head — degrading
    the candidates before the reranker ever sees them. Clamped here rather than
    at config-validation time because granularity is per-request.
    """
    pool = max(int(getattr(settings, "search_rerank_pool_size", 200)), floor)
    if grouped:
        pool = min(pool, MAX_DOCUMENT_PREFETCH // DOCUMENT_PREFETCH_FACTOR)
    return max(pool, floor)


async def rerank_results(
    results: list[SearchResult],
    query: str,
    *,
    settings: Any,
    surface: str,
) -> tuple[list[SearchResult], bool]:
    """Reorder ``results`` by cross-encoder relevance.

    Returns ``(results, reranked)``. ``reranked`` is False whenever the input
    order was preserved — disabled, unavailable, in cooldown, or degraded — so
    the caller can report honestly which ordering it is returning.
    """
    client = await _get_client(settings)
    if client is None or len(results) < 2:
        return results, False

    global _cooldown_until
    now = anyio.current_time()
    if now < _cooldown_until:
        logger.debug("rerank skipped: in failure cooldown")
        record_rerank_documents(client.model, len(results), "degraded")
        return results, False

    # Rows with no text cannot be scored; they keep retrieval order at the tail
    # rather than being handed to the model, which would rank an empty string
    # last anyway and waste a slot.
    scorable = [(i, r) for i, r in enumerate(results) if (r.excerpt or "").strip()]
    if len(scorable) < 2:
        return results, False

    started = anyio.current_time()
    try:
        with trace_operation(
            "search.rerank",
            attributes={
                "rerank.documents": len(scorable),
                "rerank.model": client.model,
                "search.surface": surface,
            },
        ):
            async with _get_limiter(settings):
                ranking = await client.rerank(query, [r.excerpt for _, r in scorable])
    except RerankError as e:
        # Degrade, never fail. The cooldown keeps an outage from costing every
        # subsequent search a full timeout.
        _cooldown_until = anyio.current_time() + _FAILURE_COOLDOWN_SECONDS
        logger.warning("rerank unavailable, using retrieval order: %s", e)
        record_rerank_documents(client.model, len(scorable), "degraded")
        return results, False

    record_search_stage(surface, "rerank", anyio.current_time() - started)
    record_rerank_documents(client.model, len(scorable), "success")

    ordered: list[SearchResult] = []
    taken: set[int] = set()
    for entry in ranking:
        original_index, result = scorable[entry.index]
        result.rerank_score = entry.score
        ordered.append(result)
        taken.add(original_index)

    # Anything the reranker did not score — unscorable rows, and any index the
    # provider omitted — is APPENDED in retrieval order, never dropped. Dropping
    # would read as a ranking change while actually being lost recall.
    ordered.extend(r for i, r in enumerate(results) if i not in taken)
    return ordered, True
