"""Best-effort usage-event recording for per-tenant metering (Deck #67).

A tenant Pod records billable operations (embedding queries, pages/chunks
embedded) into the app-DB ``usage_events`` table; the control plane later pulls
that table read-only into the billing ledger and syncs to Stripe Meter Events
(see control-plane ``usage-metering.md``). This module owns only the data-plane
recording side.

Design contract:

- **Flag-gated.** Writes are a no-op unless ``USAGE_METERING_ENABLED`` is true,
  so OSS self-hosters and unmetered deployments do zero DB work.
- **Best-effort.** A metering-write failure is logged and dropped, never raised
  into the user-facing operation. ``ON CONFLICT (event_id) DO NOTHING`` makes a
  retried write a no-op.
- **Engine reuse.** Rather than opening its own engine, this store borrows the
  process-wide :class:`RefreshTokenStorage` singleton (``get_shared_storage()``)
  — same app DB, NullPool, dialect handling, and ``_DBConn`` shim. The shared
  storage guarantees Alembic migrations (incl. ``usage_events``) already ran.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import anyio

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage, get_shared_storage
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.observability.metrics import record_db_operation

logger = logging.getLogger(__name__)


# Parameters bind untyped through the ``sa.text(...)`` shim; asyncpg infers
# each placeholder's type from its target column. For ``occurred_at``
# (TIMESTAMPTZ) it wants a real ``datetime`` (a string is rejected even with a
# CAST), so we bind the aware datetime object on Postgres; SQLite's sqlite3
# driver can't bind a ``datetime`` on Python 3.12+, so we bind an ISO string
# there. ``metadata`` (JSONB) takes a JSON string on both — asyncpg's jsonb
# codec accepts ``str`` directly, so no cast is needed. Same SQL both ways;
# only the ``occurred_at`` bind value differs by dialect.
_INSERT_SQL = (
    "INSERT INTO usage_events (event_id, occurred_at, metric, value, metadata) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT (event_id) DO NOTHING"
)


class UsageEventStore:
    """Append-only writer for the app-DB ``usage_events`` table."""

    # Process-wide cached instance returned by ``shared()`` so the hot search
    # path doesn't allocate a fresh wrapper per metered query. The store is
    # stateless beyond its storage handle, so one instance is reusable.
    # ``anyio.Lock()`` doesn't bind to an event loop at construction, so a
    # class-level instance is safe to define here (mirrors
    # ``get_shared_storage``'s ``_shared_lock``).
    _shared_instance: "UsageEventStore | None" = None
    _shared_lock: anyio.Lock = anyio.Lock()

    def __init__(self, storage: RefreshTokenStorage) -> None:
        self._storage = storage

    @classmethod
    async def shared(cls) -> "UsageEventStore":
        """Return the process-wide store backed by the storage singleton.

        Cached after first build: ``get_shared_storage()`` already returns the
        cached :class:`RefreshTokenStorage` (running ``initialize()`` / Alembic
        on first access, so ``usage_events`` exists), and the wrapper itself is
        stateless, so reusing one instance avoids a per-call allocation on the
        ``nc_semantic_search`` hot path. The lock mirrors ``get_shared_storage``
        so two concurrent cold-start callers don't both build (and one silently
        overwrite) the instance.

        Tests should construct ``UsageEventStore(storage)`` directly rather than
        via ``shared()``: the cache is a process global with no teardown hook,
        so a test that called ``shared()`` would leak its storage into the next.
        """
        async with cls._shared_lock:
            if cls._shared_instance is None:
                cls._shared_instance = cls(await get_shared_storage())
        return cls._shared_instance

    async def record_usage_event(
        self,
        *,
        metric: str,
        value: int,
        occurred_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Record one billable usage event (best-effort, flag-gated).

        Does nothing unless ``USAGE_METERING_ENABLED`` is true. Any failure is
        logged and swallowed — this must never break the caller's operation.

        Args:
            metric: Catalog metric, e.g. ``"tokens_embedded"`` or
                ``"pages_embedded"``.
            value: Count/quantity for this event.
            occurred_at: Operation completion time; defaults to now (UTC).
            metadata: Optional rawest-unit context (provider, model, tokens,
                doc_type, ...). Stored as JSONB (Postgres) / JSON text (SQLite).
            event_id: Optional idempotency key; defaults to a fresh UUID4.
            enabled: The resolved ``USAGE_METERING_ENABLED`` value. ``None``
                (default) re-reads it via ``get_settings()`` so the store stays
                self-gating for standalone/test use. Hot-path callers that
                already hold the flag should pass it to avoid a second uncached
                ``Settings`` build (``get_settings()`` is non-cached per
                ADR-024 and ``nc_semantic_search`` is on the query path).
        """
        if enabled is None:
            enabled = get_settings().usage_metering_enabled
        if not enabled:
            return

        start = time.time()
        try:
            event_id = event_id or str(uuid.uuid4())
            when = occurred_at or datetime.now(timezone.utc)
            # asyncpg takes the datetime object directly; sqlite3 needs a string.
            when_bind = (
                when if self._storage.dialect == "postgresql" else when.isoformat()
            )
            # json.dumps lives inside the best-effort try: a non-serializable
            # metadata dict must be swallowed like any other write failure, not
            # raised into the caller's operation (see the contract above).
            params = (
                event_id,
                when_bind,
                metric,
                value,
                json.dumps(metadata, sort_keys=True) if metadata is not None else None,
            )
            async with self._storage.acquire() as db:
                await db.execute(_INSERT_SQL, params)
                await db.commit()
            record_db_operation(
                self._storage.dialect, "insert", time.time() - start, "success"
            )
        except Exception:
            # Best-effort: never surface a metering failure to the user op.
            record_db_operation(
                self._storage.dialect, "insert", time.time() - start, "error"
            )
            logger.warning(
                "usage metering write dropped (metric=%s, value=%s)",
                metric,
                value,
                exc_info=True,
            )
