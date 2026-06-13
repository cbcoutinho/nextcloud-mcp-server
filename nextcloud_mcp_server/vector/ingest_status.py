"""Shared read model for the vector-sync status surface (Deck #183).

The status endpoints (``/api/v1/vector-sync/status``, the userinfo route, and
the ``nc_get_vector_sync_status`` MCP tool) all need the same "how much work is
outstanding" figure, computed differently per ``INGEST_QUEUE`` backend:

- ``memory`` — the in-process anyio stream's buffer depth (today's behavior).
- ``postgres`` — procrastinate job counts read from the per-tenant Postgres
  (``todo`` + ``doing``), plus the per-status breakdown for observability.

``indexed_documents`` (the Qdrant placeholder count) is backend-independent and
stays at each call site.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class IngestPending:
    """Outstanding-work view for the active ingest queue backend."""

    pending: int
    # Per-status counts (todo/doing/failed/…) on the postgres backend; None on
    # the memory backend, which has no durable per-status breakdown.
    job_counts: dict[str, int] | None = None
    # Per-tier-queue breakdown ``{queue: {status: count}}`` on the postgres
    # backend (Deck #323); None on the memory backend. Feeds the per-tier status
    # surface + the astrolabe_ingest_queue_depth gauge.
    job_counts_by_queue: dict[str, dict[str, int]] | None = None


async def get_ingest_pending(
    *, task_producer: Any, document_receive_stream: Any, ingest_queue: str | None
) -> IngestPending:
    """Compute outstanding ingest work for the configured queue backend.

    ``task_producer`` and ``document_receive_stream`` are intentionally typed
    ``Any``: they're duck-typed across backends. Only ``ProcrastinateTaskProducer``
    exposes ``job_counts`` (the ``TaskProducer`` protocol doesn't), and the memory
    backend reads the anyio stream's ``statistics()`` — so no single concrete type
    or Protocol fits both branches, and we probe with ``hasattr`` instead.

    Never raises — a status surface must stay available even if the queue is
    unreachable; failures degrade to ``pending=0``.
    """
    if ingest_queue == "postgres":
        counts: dict[str, int] = {}
        by_queue: dict[str, dict[str, int]] | None = None
        # Prefer the per-queue breakdown (Deck #323) and aggregate from it, so the
        # fleet-wide totals and the per-tier view always agree. Fall back to the
        # aggregated call for any producer that predates job_counts_by_queue.
        if task_producer is not None and hasattr(task_producer, "job_counts_by_queue"):
            try:
                by_queue = await task_producer.job_counts_by_queue()
                for per_status in by_queue.values():
                    for status, value in per_status.items():
                        counts[status] = counts.get(status, 0) + value
            except Exception as e:
                logger.warning("Failed to read ingest job counts by queue: %s", e)
        elif task_producer is not None and hasattr(task_producer, "job_counts"):
            try:
                counts = await task_producer.job_counts()
            except Exception as e:
                logger.warning("Failed to read ingest job counts: %s", e)
        pending = counts.get("todo", 0) + counts.get("doing", 0)
        return IngestPending(
            pending=pending, job_counts=counts, job_counts_by_queue=by_queue
        )

    if document_receive_stream is None:
        return IngestPending(pending=0)
    return IngestPending(
        pending=document_receive_stream.statistics().current_buffer_used
    )
