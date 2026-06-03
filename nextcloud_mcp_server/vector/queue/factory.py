"""Composition root for the ingest producer (Deck #183).

The transport is selected from ``INGEST_QUEUE``:

- ``postgres`` → :class:`ProcrastinateTaskProducer`, which defers jobs into the
  per-tenant Postgres for the out-of-process ``worker`` role to drain.
- ``memory`` (SQLite/dev default) → the in-process anyio stream, built inline by
  the server lifespan (it owns both the send and receive ends), so it is not
  produced here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...config import Settings

if TYPE_CHECKING:
    from .procrastinate import ProcrastinateTaskProducer

logger = logging.getLogger(__name__)


async def build_producer(settings: Settings) -> ProcrastinateTaskProducer:
    """Build the Postgres (procrastinate) ingest producer.

    Returns the concrete :class:`ProcrastinateTaskProducer` (not just the
    ``TaskProducer`` protocol) so the lifespan can call ``ensure_schema()`` on
    the open connector. Precondition: ``settings.ingest_queue == "postgres"``
    (the memory transport is constructed inline by the lifespan because it needs
    the paired receive stream for the in-process processor pool).
    """
    if settings.ingest_queue != "postgres":
        raise ValueError(
            "build_producer is only for INGEST_QUEUE=postgres; the memory "
            f"transport is built inline by the lifespan (got {settings.ingest_queue!r})"
        )

    from .procrastinate import ProcrastinateTaskProducer  # noqa: PLC0415

    return await ProcrastinateTaskProducer.connect()
