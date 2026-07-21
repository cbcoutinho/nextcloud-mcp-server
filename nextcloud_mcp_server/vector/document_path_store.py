"""Per-user display paths for shared documents (ADR-033 Phase 2, Deck #737).

A file visible to more than one user is mounted at a *different* path per user,
but the deduped Qdrant point set stores a single scalar ``file_path`` (pinned to
the owner in Phase 1). This store holds each reader's own mount path in the
``document_paths`` app-DB table, keyed on ``(doc_type, doc_id, user_id)``, and is
joined onto the returned search results *after* retrieval so every reader sees
their own path.

It is a derived, **non-security** cache: Qdrant remains the system of record, and
a missing/stale row degrades a *displayed* path only — never a permission or a
retrieval result. The methods here surface errors normally (so a caller can see a
genuine DB failure); the *best-effort* contract — a failed write is logged, not
raised — is applied at the write sites (`scanner.py` wraps the upsert, and
`release_document_for_user` the delete), since those run on the sync/search hot
paths where a display-cache hiccup must never be fatal. A new write site must
wrap the call the same way. The reader (`get_paths_for_user`, via
`verify_search_results._apply_user_display_paths`) likewise degrades to the
Qdrant scalar when a row is absent or the lookup fails.

Engine reuse mirrors :class:`BatchOcrJobStore` /
:class:`~nextcloud_mcp_server.usage.store.UsageEventStore`: rather than open its
own engine this store borrows the process-wide :class:`RefreshTokenStorage`
singleton (``get_shared_storage()``) — same app DB, dialect handling,
``?``-placeholder shim, and the guarantee that Alembic migrations (incl.
``document_paths``) already ran.
"""

from __future__ import annotations

import logging
import time

import anyio

from nextcloud_mcp_server.auth.storage import RefreshTokenStorage, get_shared_storage

logger = logging.getLogger(__name__)


class DocumentPathStore:
    """CRUD for the ``document_paths`` table (one row per document + reader)."""

    _shared_instance: DocumentPathStore | None = None
    # Lazy-init: anyio primitives must not be created at import time (CLAUDE.md;
    # mirrors BatchOcrJobStore). Created on first shared() call.
    _shared_lock: anyio.Lock | None = None

    def __init__(self, storage: RefreshTokenStorage) -> None:
        self._storage = storage

    @classmethod
    async def shared(cls) -> DocumentPathStore:
        """Process-wide store backed by the storage singleton. Tests should
        construct ``DocumentPathStore(storage)`` directly — the cache is a process
        global with no teardown hook."""
        # No await between the None-check and the assignment, so this is atomic
        # within the single event loop (anyio is cooperative).
        if cls._shared_lock is None:
            cls._shared_lock = anyio.Lock()
        async with cls._shared_lock:
            if cls._shared_instance is None:
                cls._shared_instance = cls(await get_shared_storage())
        return cls._shared_instance

    async def upsert(
        self,
        *,
        user_id: str,
        doc_id: str,
        doc_type: str,
        file_path: str,
        updated_at: int | None = None,
    ) -> None:
        """Record ``user_id``'s mount path for a document (insert or overwrite).

        Idempotent: re-observing the same path rewrites the row with the same
        value (and a fresh ``updated_at``). ``ON CONFLICT DO UPDATE`` is supported
        by both SQLite and Postgres; ``excluded`` references the row that would
        have been inserted.
        """
        now = updated_at if updated_at is not None else int(time.time())
        async with self._storage.acquire() as db:
            await db.execute(
                "INSERT INTO document_paths "
                "(doc_type, doc_id, user_id, file_path, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (doc_type, doc_id, user_id) DO UPDATE SET "
                "file_path = excluded.file_path, updated_at = excluded.updated_at",
                (doc_type, doc_id, user_id, file_path, now),
            )
            await db.commit()

    async def get_paths_for_user(
        self,
        user_id: str,
        doc_type: str,
        doc_ids: list[str],
    ) -> dict[str, str]:
        """Return ``{doc_id: file_path}`` for a user's returned docs of one type.

        ``doc_ids`` is the id list of the current result page (bounded by the
        search limit), so the ``IN`` list is small. Callers only ever query one
        ``doc_type`` at a time (only files carry per-user paths), and the query
        constrains on it so a stored row for a different type whose id collides
        with this one's can never be returned. Documents with no stored row are
        simply absent — the caller falls back to the Qdrant scalar for those.
        """
        if not doc_ids:
            return {}
        # De-dupe while preserving the small bounded size; a result page can carry
        # several chunks of the same document.
        unique_ids = sorted(set(doc_ids))
        placeholders = ", ".join("?" for _ in unique_ids)
        sql = (
            "SELECT doc_id, file_path FROM document_paths "
            f"WHERE user_id = ? AND doc_type = ? AND doc_id IN ({placeholders})"
        )
        params: list[str] = [user_id, doc_type, *unique_ids]
        result: dict[str, str] = {}
        async with self._storage.acquire() as db:
            async with db.execute(sql, params) as cursor:
                for row in await cursor.fetchall():
                    result[row[0]] = row[1]
        return result

    async def delete(self, *, user_id: str, doc_id: str, doc_type: str) -> None:
        """Drop a reader's path row (e.g. when they release the document)."""
        async with self._storage.acquire() as db:
            await db.execute(
                "DELETE FROM document_paths "
                "WHERE doc_type = ? AND doc_id = ? AND user_id = ?",
                (doc_type, doc_id, user_id),
            )
            await db.commit()
