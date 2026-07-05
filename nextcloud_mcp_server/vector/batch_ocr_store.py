"""Tracking store for in-flight async batch OCR jobs (Deck #332).

When ``DOCUMENT_OCR_MODE=batch`` the OCR tier submits a document to the gateway's
async batch route and then re-polls across procrastinate retries. procrastinate
job args are immutable, so the gateway ``job_id`` (and submit time, for job-age
observability) live in the ``batch_ocr_jobs`` app-DB table, keyed on the document +
its content version (``etag``). A pending job is polled indefinitely — the gateway
owns the OCR lifecycle, so there is no worker-side give-up deadline (Deck #523).

Engine reuse mirrors :class:`~nextcloud_mcp_server.usage.store.UsageEventStore`:
rather than open its own engine this store borrows the process-wide
:class:`RefreshTokenStorage` singleton (``get_shared_storage()``) — same app DB,
dialect handling, ``?``-placeholder shim, and the guarantee that Alembic
migrations (incl. ``batch_ocr_jobs``) already ran.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import anyio

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage, get_shared_storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchOcrJob:
    """A tracked in-flight batch OCR job. A row exists only while pending (terminal
    jobs are deleted), so there's no stored status — the live status comes from a
    fresh ``GatewayBatchOcrClient.poll``. ``submitted_at`` records the submit time
    (observability / job age); a pending job is polled indefinitely, so it no longer
    anchors a give-up deadline (Deck #523)."""

    job_id: str
    submitted_at: int


class BatchOcrJobStore:
    """CRUD for the ``batch_ocr_jobs`` table (one row per in-flight job)."""

    _shared_instance: BatchOcrJobStore | None = None
    # Lazy-init: anyio primitives must not be created at import time (CLAUDE.md;
    # mirrors OcrProcessor._backend_lock). Created on first shared() call.
    _shared_lock: anyio.Lock | None = None

    def __init__(self, storage: RefreshTokenStorage) -> None:
        self._storage = storage

    @classmethod
    async def shared(cls) -> BatchOcrJobStore:
        """Process-wide store backed by the storage singleton. Tests should
        construct ``BatchOcrJobStore(storage)`` directly — the cache is a process
        global with no teardown hook."""
        # No await between the None-check and the assignment, so this is atomic
        # within the single event loop (anyio is cooperative) — two cold-start
        # callers can't both create a lock.
        if cls._shared_lock is None:
            cls._shared_lock = anyio.Lock()
        async with cls._shared_lock:
            if cls._shared_instance is None:
                cls._shared_instance = cls(await get_shared_storage())
        return cls._shared_instance

    async def get(
        self, *, user_id: str, doc_id: str, doc_type: str, etag: str
    ) -> BatchOcrJob | None:
        """The in-flight job for this document+version, or ``None``."""
        async with self._storage.acquire() as db:
            async with db.execute(
                "SELECT job_id, submitted_at FROM batch_ocr_jobs "
                "WHERE user_id = ? AND doc_id = ? AND doc_type = ? AND etag = ?",
                (user_id, doc_id, doc_type, etag),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return BatchOcrJob(job_id=row[0], submitted_at=int(row[1]))

    async def insert_pending(
        self,
        *,
        user_id: str,
        doc_id: str,
        doc_type: str,
        etag: str,
        job_id: str,
        submitted_at: int | None = None,
    ) -> None:
        """Record a freshly-submitted job. ``ON CONFLICT DO NOTHING`` makes a
        racing double-submit harmless (the first row wins; the loser's job id is
        abandoned and reaped by the gateway-side file purge)."""
        now = submitted_at if submitted_at is not None else int(time.time())
        async with self._storage.acquire() as db:
            await db.execute(
                "INSERT INTO batch_ocr_jobs "
                "(user_id, doc_id, doc_type, etag, job_id, submitted_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id, doc_id, doc_type, etag) DO NOTHING",
                (user_id, doc_id, doc_type, etag, job_id, now),
            )
            await db.commit()

    async def delete(
        self, *, user_id: str, doc_id: str, doc_type: str, etag: str
    ) -> None:
        """Drop the row once the job is terminal (succeeded or failed)."""
        async with self._storage.acquire() as db:
            await db.execute(
                "DELETE FROM batch_ocr_jobs "
                "WHERE user_id = ? AND doc_id = ? AND doc_type = ? AND etag = ?",
                (user_id, doc_id, doc_type, etag),
            )
            await db.commit()

    async def delete_stale_for_doc(
        self, *, user_id: str, doc_id: str, doc_type: str, keep_etag: str
    ) -> None:
        """Remove superseded-version rows for a document (any etag other than the
        current one) before a resubmit, so a re-edited file doesn't leave its
        old in-flight job tracked forever."""
        async with self._storage.acquire() as db:
            await db.execute(
                "DELETE FROM batch_ocr_jobs "
                "WHERE user_id = ? AND doc_id = ? AND doc_type = ? AND etag != ?",
                (user_id, doc_id, doc_type, keep_etag),
            )
            await db.commit()
