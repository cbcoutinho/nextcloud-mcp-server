"""Content-addressed dead-letter markers for terminally-failed documents.

A document that fails its terminal extraction tier (a hard parse failure with no
higher tier to escalate to — e.g. the ``structured`` tier timing out while OCR is
disabled) must not be retried forever. The per-user placeholder ``status="failed"``
mark cannot stop the loop on its own: placeholder point IDs are user-agnostic
(``uuid5("file:<doc_id>:placeholder")``) but the scanner's freshness gate filters
by ``user_id``, so for a file visible to *several* users the single shared
placeholder's ``user_id`` is overwritten by whoever scanned last and every other
user's scan sees "no record → re-queue", re-burning the (failing) parse on a loop.

This module records a **durable, content-addressed, user-agnostic** dead-letter
marker instead. One marker point per document (a distinct deterministic ID, kept
separate from the in-flight placeholder), carrying the ``etag`` and an escalation
``tiers_sig`` (see ``document_processors.escalation.escalation_tiers_signature``).
The scanner consults it tenant-wide — for every user — and skips re-queuing while
BOTH still match, so the document is attempted once per content-version and never
loops. A content change (new ``etag``) or a config change that adds an escalation
tier (e.g. enabling OCR — new ``tiers_sig``) makes the marker stale and the
document retryable again.

The marker carries ``is_placeholder=True`` so the existing search exclusion
(``get_placeholder_filter``) keeps it out of user-facing results with no extra
filter, plus ``dead_letter=True`` so the orphan-placeholder sweep and the scanner
can tell it apart from a volatile in-flight placeholder. Mirrors the fail-safe
philosophy of ``sharing_state``: a Qdrant error never aborts ingest — a failed
lookup degrades to "process normally", a failed write is logged, not raised.

TODO(deck-349): a marker for a file that is dead-lettered and *then* deleted from
Nextcloud can be orphaned. The processor's delete path clears it, but the
scanner's grace-period deletion tracking only sees a file via its real indexed
points (filtered by ``user_id``); a dead-lettered file has only this
user-agnostic marker, so its disappearance enqueues no delete task and the marker
is never reached. Not a correctness issue (search excludes ``is_placeholder=True``
and the etag check means a stale marker never blocks new content), but it
accumulates. A dedicated marker sweep or a TTL payload field would close it —
tracked as a follow-up, not done here.
"""

import logging
import time
import uuid
from typing import Any

from qdrant_client import models
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

# Payload flag distinguishing a durable dead-letter marker from an in-flight
# placeholder (both carry is_placeholder=True to inherit the search exclusion).
DEAD_LETTER_KEY = "dead_letter"


def _generate_dead_letter_id(doc_type: str, doc_id: str) -> str:
    """Deterministic, user-agnostic point ID for a document's dead-letter marker.

    Distinct from the in-flight placeholder ID (``…:placeholder``) so the two can
    coexist briefly and never collide; one marker per ``(doc_type, doc_id)``, so
    a re-failure upserts in place rather than accumulating.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc_type}:{doc_id}:deadletter"))


def _dead_letter_filter(doc_id: str, doc_type: str) -> Filter:
    """Match the dead-letter marker for one document (tenant-wide, no user_id).

    Includes ``is_placeholder=True`` (redundant with ``dead_letter=True``, which
    nothing else sets) to inherit the search exclusion. Note both fields must be
    payload-indexed: Qdrant strict mode requires an index for *every* condition
    in a filter, so ``dead_letter`` carries its own index (registered in
    ``qdrant_client._PAYLOAD_INDEX_FIELDS``) — without it this scroll 400s and
    ``is_dead_lettered`` fail-opens, re-queuing the document forever.
    """
    return Filter(
        must=[
            FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
            FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
            FieldCondition(key="is_placeholder", match=MatchValue(value=True)),
            FieldCondition(key=DEAD_LETTER_KEY, match=MatchValue(value=True)),
        ]
    )


async def mark_dead_letter(
    doc_id: str,
    doc_type: str,
    etag: str,
    tiers_sig: str,
    reason: str,
    *,
    file_path: str | None = None,
) -> None:
    """Upsert a durable dead-letter marker for a terminally-failed document.

    Keyed by content (``etag``) + escalation config (``tiers_sig``); the scanner
    skips re-queuing while both match. ``reason`` is the parse failure reason
    (``timeout`` | ``oom`` | ``error``). Fail-safe: a Qdrant error is logged, not
    raised — a missed mark just means the document is retried (the bounded prior
    behaviour), never a crash.
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()
        # Match the collection's dense slot, which is always sized from the
        # embedding service (mirrors placeholder.py / collection creation).
        dimension = get_embedding_service().get_dimension()

        payload: dict[str, Any] = {
            "doc_id": doc_id,
            "doc_type": doc_type,
            "is_placeholder": True,
            DEAD_LETTER_KEY: True,
            "etag": etag,
            "tiers_sig": tiers_sig,
            "reason": reason,
            "failed_at": int(time.time()),
        }
        if doc_type == "file" and file_path:
            payload["file_path"] = file_path

        point = PointStruct(
            id=_generate_dead_letter_id(doc_type, doc_id),
            vector={
                "dense": [0.0] * dimension,
                "sparse": models.SparseVector(indices=[], values=[]),
            },
            payload=payload,
        )
        await qdrant_client.upsert(
            collection_name=settings.get_collection_name(),
            points=[point],
            wait=True,
        )
        logger.info(
            "Dead-lettered %s_%s (reason=%s, etag=%s)",
            doc_type,
            doc_id,
            reason,
            etag,
        )
    except Exception as e:
        logger.warning(
            "Failed to write dead-letter marker for %s_%s: %s", doc_type, doc_id, e
        )
        # Don't raise — dead-lettering is best-effort; a miss just retries.


async def is_dead_lettered(
    doc_id: str,
    doc_type: str,
    etag: str,
    tiers_sig: str,
) -> bool:
    """Whether this exact content is currently dead-lettered (skip re-queuing).

    Returns True only when a marker exists for ``(doc_id, doc_type)`` whose stored
    ``etag`` AND ``tiers_sig`` both match the current values — so a content change
    or a new escalation tier (e.g. OCR enabled) makes the document retryable
    again. An empty ``etag`` is never dead-lettered (we cannot content-address it).
    Fail-safe: a Qdrant error degrades to False (process normally), mirroring
    ``sharing_state.claim_existing_index``.
    """
    if not etag:
        return False
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()
        points, _ = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=_dead_letter_filter(doc_id, doc_type),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        logger.warning(
            "Dead-letter lookup failed for %s_%s (%s); processing normally",
            doc_type,
            doc_id,
            e,
        )
        return False
    if not points:
        return False
    payload = dict(points[0].payload or {})
    return payload.get("etag") == etag and payload.get("tiers_sig") == tiers_sig


async def clear_dead_letter(doc_id: str, doc_type: str) -> None:
    """Delete a document's dead-letter marker (on successful index / release).

    Idempotent and fail-safe: deleting a non-existent marker is a Qdrant no-op,
    and an error is logged rather than raised so it never breaks the indexing
    path that calls it.
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()
        await qdrant_client.delete(
            collection_name=settings.get_collection_name(),
            points_selector=_dead_letter_filter(doc_id, doc_type),
        )
        logger.debug("Cleared dead-letter marker for %s_%s", doc_type, doc_id)
    except Exception as e:
        logger.warning(
            "Failed to clear dead-letter marker for %s_%s: %s", doc_type, doc_id, e
        )
