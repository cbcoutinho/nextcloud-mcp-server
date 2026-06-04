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
from nextcloud_mcp_server.observability.metrics import (
    update_vector_sync_indexed_chunks,
    update_vector_sync_indexed_documents,
    update_vector_sync_pending_documents,
    update_vector_sync_queue_size,
)
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
