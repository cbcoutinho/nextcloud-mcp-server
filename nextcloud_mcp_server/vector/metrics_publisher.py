"""Periodic publisher for vector-sync outstanding-work + corpus-size gauges.

Why a dedicated task instead of updating the gauges inline? The per-loop update
of ``mcp_vector_sync_queue_size`` only runs in the single-user consumer
(``processor_task``). The multi-user consumer (``oauth_processor_task``) drains
the same queue but never touched the gauge, so in multi-user deployments the
gauge read 0 while the live anyio buffer held thousands of pending documents
(observed on tenant-blackbox-demo: gauge 0 for 24h vs 2214 pending in the status
endpoint). This task publishes the *same* ``get_ingest_pending()`` figure the
``/api/v1/vector-sync/status`` endpoint serves, on a fixed cadence, independent
of which consumer drains the queue and of the queue backend (anyio buffer depth
or procrastinate ``todo+doing``).

It also publishes corpus size split into documents vs chunks. ``indexed_chunks``
is every non-placeholder point; ``indexed_documents`` is the distinct document
count, obtained exactly and cheaply by counting the ``chunk_index=0`` point each
document has (both fields are payload-indexed), avoiding a Qdrant facet pass.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
from anyio.abc import TaskStatus
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.observability.metrics import (
    CHUNK_DENSITY_BUCKETS,
    density_bucket_index,
    estimate_vector_bytes,
    update_ingest_queue_depth,
    update_qdrant_chunk_density_snapshot,
    update_vector_sync_estimated_vector_bytes,
    update_vector_sync_indexed_chunks,
    update_vector_sync_indexed_documents,
    update_vector_sync_pending_documents,
    update_vector_sync_qdrant_vector_bytes,
    update_vector_sync_qdrant_vectors,
    update_vector_sync_queue_size,
)
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.ingest_status import get_ingest_pending
from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


async def count_indexed(
    qdrant_client: AsyncQdrantClient, collection: str, *, exact: bool = True
) -> tuple[int, int]:
    """Return ``(documents, chunks)`` indexed in the collection.

    ``chunks`` is every non-placeholder point; ``documents`` is the distinct
    document count via the ``chunk_index=0`` point each document carries (no
    facet needed). Excludes in-flight placeholder points.

    ``exact`` is forwarded to Qdrant ``count``: the periodic gauge publisher
    passes ``exact=False`` so the every-N-seconds refresh stays O(1)-ish on
    large tenants, while the on-demand status endpoint keeps the default
    ``exact=True`` for an accurate user-facing figure.
    """
    chunks_result = await qdrant_client.count(
        collection_name=collection,
        count_filter=Filter(must=[get_placeholder_filter()]),
        exact=exact,
    )
    docs_result = await qdrant_client.count(
        collection_name=collection,
        count_filter=Filter(
            must=[
                get_placeholder_filter(),
                FieldCondition(key="chunk_index", match=MatchValue(value=0)),
            ]
        ),
        exact=exact,
    )
    return docs_result.count, chunks_result.count


async def count_hybrid_chunks(
    qdrant_client: AsyncQdrantClient, collection: str, *, exact: bool = True
) -> int:
    """Return the number of hybrid (dense-bearing) chunks in the collection.

    Only ``index_mode == hybrid`` points carry a dense vector; keyword-index
    points are sparse-only and cost no dense-vector RAM. This is the count that
    drives the RAM estimate. Excludes in-flight placeholder points.
    """
    result = await qdrant_client.count(
        collection_name=collection,
        count_filter=Filter(
            must=[
                get_placeholder_filter(),
                FieldCondition(
                    key=payload_keys.INDEX_MODE,
                    match=MatchValue(value=payload_keys.INDEX_MODE_HYBRID),
                ),
            ]
        ),
        exact=exact,
    )
    return result.count


async def estimate_hybrid_vector_bytes(
    qdrant_client: AsyncQdrantClient,
    collection: str,
    overhead: float,
    *,
    exact: bool = True,
) -> tuple[int, int]:
    """Return ``(hybrid_chunks, estimated_vector_bytes)`` for the collection.

    Single source of truth for the dense-vector RAM figure surfaced by the MCP
    tool and the ``/api/v1/vector-sync/status`` HTTP route, so the two can't
    drift. ``overhead`` is ``settings.vector_ram_hnsw_overhead_factor``;
    ``estimated_vector_bytes`` is ``hybrid_chunks * dim * 4 * overhead`` (card
    #624), rounded to an int for the response payloads.
    """
    hybrid_chunks = await count_hybrid_chunks(qdrant_client, collection, exact=exact)
    dim = get_embedding_service().get_dimension()
    estimated = int(estimate_vector_bytes(hybrid_chunks, dim, overhead))
    return hybrid_chunks, estimated


async def publish_vector_sync_metrics(
    task_producer: Any, document_receive_stream: Any
) -> None:
    """Compute and publish one snapshot of the vector-sync gauges.

    Never raises: a metrics refresh must not disturb the ingest pipeline. Each
    figure is published independently so a failure in one (e.g. Qdrant briefly
    unreachable) does not block the others.
    """
    settings = get_settings()

    # Outstanding work — the same figure the status endpoint serves, so the
    # Prometheus gauge and the Astrolabe UI never disagree.
    try:
        pending = await get_ingest_pending(
            task_producer=task_producer,
            document_receive_stream=document_receive_stream,
            ingest_queue=settings.ingest_queue,
        )
        update_vector_sync_pending_documents(pending.pending)
        # Keep the legacy gauge meaningful on every consumer path, not just the
        # single-user one — existing dashboards/alerts reference it.
        update_vector_sync_queue_size(pending.pending)
        # Per-tier-queue depth (Deck #323): None on the memory backend (no-op).
        update_ingest_queue_depth(pending.job_counts_by_queue)
    except Exception as exc:  # noqa: BLE001 — metrics must not break ingest
        logger.warning("Failed to publish pending-documents gauge: %s", exc)

    # Corpus size — documents and chunks separately (the chunk fan-out makes a
    # single "indexed" number ambiguous).
    try:
        qdrant_client = await get_qdrant_client()
        # Approximate is plenty for a gauge refreshed every few seconds and keeps
        # the cost bounded on large tenants; the status endpoint counts exactly.
        documents, chunks = await count_indexed(
            qdrant_client, settings.get_collection_name(), exact=False
        )
        update_vector_sync_indexed_documents(documents)
        update_vector_sync_indexed_chunks(chunks)
    except Exception as exc:  # noqa: BLE001 — metrics must not break ingest
        logger.warning("Failed to publish indexed-corpus gauges: %s", exc)

    # Dense-vector RAM footprint (card #624) — the real hybrid-search cost driver
    # that source-byte billing does not capture. Two independent views so the
    # deterministic estimate can be validated against Qdrant's own reported count:
    #   * estimate  = OUR hybrid chunk count (payload filter) * dim * 4 * overhead
    #   * actuals   = Qdrant vectors_count (get_collection) * dim * 4 * overhead
    # Separate try/except so a Qdrant hiccup here never blocks the gauges above.
    try:
        qdrant_client = await get_qdrant_client()
        collection = settings.get_collection_name()
        dim = get_embedding_service().get_dimension()
        overhead = settings.vector_ram_hnsw_overhead_factor

        hybrid_chunks = await count_hybrid_chunks(
            qdrant_client, collection, exact=False
        )
        update_vector_sync_estimated_vector_bytes(
            estimate_vector_bytes(hybrid_chunks, dim, overhead)
        )

        # Qdrant's own reported count as an independent reality check.
        # ``vectors_count`` is optional/deprecated in the client model (often None);
        # ``getattr`` keeps this robust across client versions and falls back to
        # ``points_count``. It aggregates all named vectors / includes non-dense
        # points, so it is a coarse upper bound, not a clean dense-only figure —
        # the gap vs the estimate is the drift signal, not a bug.
        info = await qdrant_client.get_collection(collection)
        qdrant_count = getattr(info, "vectors_count", None)
        if qdrant_count is None:
            qdrant_count = info.points_count or 0
        update_vector_sync_qdrant_vectors(qdrant_count)
        update_vector_sync_qdrant_vector_bytes(
            estimate_vector_bytes(qdrant_count, dim, overhead)
        )
    except Exception as exc:  # noqa: BLE001 — metrics must not break ingest
        logger.warning("Failed to publish vector-RAM gauges: %s", exc)


async def vector_sync_metrics_task(
    task_producer: Any,
    document_receive_stream: Any,
    shutdown_event: anyio.Event,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Publish the vector-sync gauges every ``vector_sync_metrics_refresh_interval``.

    Spawned in every deployment mode and queue backend so the outstanding-work
    and corpus gauges are accurate regardless of which consumer drains the queue.
    ``document_receive_stream`` is None in postgres mode — ``get_ingest_pending``
    falls back to the procrastinate job counts there.
    """
    settings = get_settings()
    interval = settings.vector_sync_metrics_refresh_interval
    logger.info("Vector-sync metrics publisher started (interval=%ss)", interval)
    task_status.started()

    while not shutdown_event.is_set():
        await publish_vector_sync_metrics(task_producer, document_receive_stream)
        # Sleep until the next refresh or until shutdown, whichever comes first.
        with anyio.move_on_after(interval):
            await shutdown_event.wait()


async def compute_chunk_density_snapshot(
    qdrant_client: AsyncQdrantClient,
    collection: str,
    *,
    max_documents: int,
    page_size: int = 1000,
) -> tuple[
    dict[str, tuple[list[float], float]], dict[str, int], bool, dict[str, float]
]:
    """Tally the current-corpus chunk-density distribution by scrolling Qdrant.

    Iterates the ``chunk_index == 0`` point of every non-placeholder document
    (one point per document — the same trick ``count_indexed`` uses), reading only
    ``total_chunks``, ``source_bytes`` and ``doc_type`` from the payload. For each
    document carrying a usable source size it computes
    ``total_chunks / (source_bytes / 1e6)`` and increments the matching
    per-``doc_type`` bucket (edges = ``CHUNK_DENSITY_BUCKETS`` + a ``+Inf``
    overflow slot). Documents with no usable ``source_bytes`` (payload predates the
    key, or a non-positive value) are tallied in ``uncovered`` instead of silently
    shrinking the histogram.

    Returns ``(per_doc_type, uncovered, truncated, source_bytes_totals)`` shaped for
    ``update_qdrant_chunk_density_snapshot``: ``per_doc_type`` maps
    ``doc_type -> (bucket_counts, gsum)``, ``uncovered`` maps ``doc_type -> count``,
    ``truncated`` is True when the scan stopped at ``max_documents`` (partial
    snapshot), and ``source_bytes_totals`` maps ``doc_type -> sum(source_bytes)`` over
    exactly the covered documents (same forward-only set as the histogram — docs
    counted in ``uncovered`` are excluded), enabling a corpus-weighted (byte-weighted)
    density in Grafana. Errors propagate to the best-effort caller.
    """
    n_slots = len(CHUNK_DENSITY_BUCKETS) + 1
    bucket_counts: dict[str, list[float]] = {}
    gsums: dict[str, float] = {}
    source_bytes_totals: dict[str, float] = {}
    uncovered: dict[str, int] = {}
    scanned = 0
    truncated = False
    offset = None

    doc_filter = Filter(
        must=[
            get_placeholder_filter(),
            FieldCondition(key="chunk_index", match=MatchValue(value=0)),
        ]
    )

    while True:
        points, offset = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=doc_filter,
            with_payload=["total_chunks", payload_keys.SOURCE_BYTES, "doc_type"],
            with_vectors=False,
            limit=page_size,
            offset=offset,
        )
        for point in points:
            payload = point.payload or {}
            doc_type = payload.get("doc_type") or "unknown"
            total_chunks = payload.get("total_chunks")
            source_bytes = payload.get(payload_keys.SOURCE_BYTES)
            # bool is an int subclass — exclude it so a stray True can't meter as
            # 1. The isinstance checks are inline (not hoisted into a bool) so the
            # type checker narrows total_chunks/source_bytes for the division.
            if (
                isinstance(total_chunks, int)
                and not isinstance(total_chunks, bool)
                and total_chunks > 0
                and isinstance(source_bytes, (int, float))
                and not isinstance(source_bytes, bool)
                and source_bytes > 0
            ):
                density = total_chunks / (source_bytes / 1_000_000)
                counts = bucket_counts.setdefault(doc_type, [0.0] * n_slots)
                counts[density_bucket_index(density)] += 1
                gsums[doc_type] = gsums.get(doc_type, 0.0) + density
                source_bytes_totals[doc_type] = (
                    source_bytes_totals.get(doc_type, 0.0) + source_bytes
                )
            else:
                uncovered[doc_type] = uncovered.get(doc_type, 0) + 1
        scanned += len(points)
        # Qdrant's end-of-scroll signal (offset is None) is authoritative and is
        # checked FIRST: reaching it means the whole collection was covered, so it
        # is never truncated — even if the final page pushed ``scanned`` to/over
        # the cap. Only when there is genuinely more to fetch (offset not None)
        # AND we have already retrieved *strictly more* than the cap do we stop
        # early and flag truncation. This tolerates one page of slop and avoids a
        # false positive when the collection size lands exactly on the cap: Qdrant
        # returns a non-None next offset even when the following scroll would come
        # back empty, so ``offset is not None`` alone is not proof of more data.
        if offset is None:
            break
        if scanned > max_documents:
            truncated = True
            break

    per_doc_type = {dt: (bucket_counts[dt], gsums[dt]) for dt in bucket_counts}
    return per_doc_type, uncovered, truncated, source_bytes_totals


async def publish_chunk_density_snapshot() -> None:
    """Compute and publish one current-corpus chunk-density snapshot.

    Never raises: a metrics refresh must not disturb ingest. On any failure the
    previously published snapshot simply remains until the next successful pass.
    """
    settings = get_settings()
    try:
        qdrant_client = await get_qdrant_client()
        (
            per_doc_type,
            uncovered,
            truncated,
            source_bytes_totals,
        ) = await compute_chunk_density_snapshot(
            qdrant_client,
            settings.get_collection_name(),
            max_documents=settings.vector_density_snapshot_max_documents,
        )
        update_qdrant_chunk_density_snapshot(
            per_doc_type,
            uncovered=uncovered,
            truncated=truncated,
            source_bytes=source_bytes_totals,
        )
        if truncated:
            logger.warning(
                "Chunk-density snapshot hit the %s-document scan cap; the "
                "published distribution covers only a prefix of the collection",
                settings.vector_density_snapshot_max_documents,
            )
    except Exception as exc:  # noqa: BLE001 — metrics must not break ingest
        logger.warning("Failed to publish chunk-density snapshot: %s", exc)


async def vector_density_snapshot_task(
    shutdown_event: anyio.Event,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Publish the current-corpus chunk-density snapshot on a slow cadence.

    Separate from ``vector_sync_metrics_task`` because the collection scroll is
    materially heavier than the ``count()``-based gauges, so it runs on its own
    (longer) ``vector_density_snapshot_interval``. Spawned only when both vector
    sync and the snapshot are enabled (see app startup wiring).
    """
    settings = get_settings()
    interval = settings.vector_density_snapshot_interval
    logger.info("Chunk-density snapshot publisher started (interval=%ss)", interval)
    task_status.started()

    while not shutdown_event.is_set():
        await publish_chunk_density_snapshot()
        # Sleep until the next snapshot or until shutdown, whichever comes first.
        with anyio.move_on_after(interval):
            await shutdown_event.wait()
