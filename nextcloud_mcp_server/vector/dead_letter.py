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

The same marker point also tracks *non-terminal* failures (GH #1345). A parse
failure at the deepest tier is terminal on its first attempt, but an embedding /
Qdrant / transport failure is transient-until-proven-otherwise: parking a document
on the first one would drop every in-flight document during a backend outage. So
``record_index_failure`` counts **consecutive** such failures per content-version
in the marker's ``attempts`` field and only flips ``dead_letter`` to True once
``VECTOR_SYNC_MAX_INDEX_FAILURES`` is reached. Below that threshold the marker is
a *soft* one: ``dead_letter=False``, invisible to ``is_dead_lettered``, so the
scanner keeps retrying. A successful index (or a delete) clears the marker, which
is what makes the count consecutive rather than cumulative.

The marker carries ``is_placeholder=True`` so the existing search exclusion
(``get_placeholder_filter``) keeps it out of user-facing results with no extra
filter, plus ``dead_letter`` (True once terminal) so the orphan-placeholder sweep
and the scanner can tell it apart from a volatile in-flight placeholder. The sweep
keys off the *presence* of that field, not its value, so a soft marker is not
swept away mid-count.

Mirrors the fail-safe philosophy of ``sharing_state``: a Qdrant error never aborts
ingest — a failed lookup degrades to "process normally", a failed write is logged,
not raised.

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
from nextcloud_mcp_server.providers import get_provider
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

# Payload flag distinguishing a durable dead-letter marker from an in-flight
# placeholder (both carry is_placeholder=True to inherit the search exclusion).
# True only when the failure is TERMINAL; a soft (still-counting) marker sets it
# False, so every existing consumer that filters on ``dead_letter=True`` -- the
# scanner's is_dead_lettered lookup and the dead-lettered-documents gauge --
# keeps counting only genuinely parked documents, with no new payload index.
DEAD_LETTER_KEY = "dead_letter"

# Consecutive-failure counter carried on the marker (GH #1345).
ATTEMPTS_KEY = "attempts"


def _generate_dead_letter_id(doc_type: str, doc_id: str) -> str:
    """Deterministic, user-agnostic point ID for a document's dead-letter marker.

    Distinct from the in-flight placeholder ID (``…:placeholder``) so the two can
    coexist briefly and never collide; one marker per ``(doc_type, doc_id)``, so
    a re-failure upserts in place rather than accumulating.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc_type}:{doc_id}:deadletter"))


def _dead_letter_filter(doc_id: str, doc_type: str) -> Filter:
    """Match the dead-letter marker for one document (tenant-wide, no user_id).

    Matches only a TERMINAL marker (``dead_letter=True``); a soft counting marker
    is deliberately invisible here so ``is_dead_lettered`` keeps returning False
    while retries are still wanted. Use ``_generate_dead_letter_id`` to reach a
    marker of either kind.

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


async def _upsert_marker(
    doc_id: str,
    doc_type: str,
    etag: str,
    tiers_sig: str,
    reason: str,
    *,
    terminal: bool,
    attempts: int,
    file_path: str | None,
) -> None:
    """Write the (soft or terminal) marker point. Fail-safe: never raises."""
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()
        # Match the collection's dense slot, which is always sized from the
        # embedding provider (mirrors placeholder.py / collection creation).
        dimension = get_provider().get_dimension()

        payload: dict[str, Any] = {
            "doc_id": doc_id,
            "doc_type": doc_type,
            "is_placeholder": True,
            DEAD_LETTER_KEY: terminal,
            ATTEMPTS_KEY: attempts,
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
    except Exception as e:
        logger.warning(
            "Failed to write dead-letter marker for %s_%s: %s", doc_type, doc_id, e
        )
        # Don't raise — dead-lettering is best-effort; a miss just retries.


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

    Terminal on the first call: a hard parse failure at the deepest available
    tier defeats a retry by construction. Repeatable *index* failures go through
    :func:`record_index_failure` instead, which parks the document only after a
    threshold. Overwrites any soft marker that path had left behind.
    """
    await _upsert_marker(
        doc_id,
        doc_type,
        etag,
        tiers_sig,
        reason,
        terminal=True,
        attempts=1,
        file_path=file_path,
    )
    logger.info(
        "Dead-lettered %s_%s (reason=%s, etag=%s)",
        doc_type,
        doc_id,
        reason,
        etag,
    )


async def record_index_failure(
    doc_id: str,
    doc_type: str,
    etag: str,
    tiers_sig: str,
    reason: str,
    *,
    file_path: str | None = None,
) -> bool:
    """Count one failed index attempt; dead-letter once the limit is reached.

    The GH #1345 path. Unlike a parse failure, an embed/Qdrant/transport failure
    is assumed transient at first — parking on the first one would drop every
    in-flight document during a backend outage. So the count is kept on the
    marker and the document is parked only at
    ``VECTOR_SYNC_MAX_INDEX_FAILURES``; below that the marker stays soft and the
    scanner keeps re-queuing, exactly as it did before this existed.

    The count is *consecutive*: a successful index or a delete clears the marker
    (``clear_dead_letter``), and a marker whose ``etag``/``tiers_sig`` no longer
    match is treated as a fresh start — new content deserves its own budget
    rather than inheriting the previous version's.

    Returns True when this failure parked the document (so the caller can record
    the dead-letter metric), False while it is still being retried. Fail-safe: a
    Qdrant read error counts the attempt as the first, which at worst delays
    parking — it never parks a document early.

    **A Qdrant outage cannot park anything**, and that is load-bearing rather
    than incidental: the counter lives in the very store whose availability it
    is judging, so while Qdrant is down both the read and the write here fail
    and the count cannot advance. Were it kept anywhere else, an outage longer
    than ``limit × VECTOR_SYNC_SCAN_INTERVAL`` would park every in-flight
    document, and they would stay parked until their etag changed — i.e. never,
    for a static corpus. Pinned by
    ``test_a_qdrant_outage_cannot_park_anything``; do not "fix" the swallowed
    errors into a fallback that keeps counting.

    Note the asymmetry: a *Qdrant* fault self-protects this way, but a sustained
    **embedding-backend** outage does not — Qdrant stays healthy, so the count
    advances normally and documents park after ``limit`` rounds. A failure that
    is genuinely per-document (an oversize payload, a chunk the backend always
    rejects) is exactly what parking is for; a backend-wide outage is not.
    Distinguishing them needs cross-document state this function does not have.

    The read-then-write is deliberately NOT compare-and-swap. Two concurrent
    failures for the same document can both read ``attempts=N`` and both write
    ``N+1``, undercounting by one — and that is reachable in normal operation,
    since a file shared by N users produces N tasks for the same ``doc_id``. The
    write is also last-write-wins on a deterministic point ID, so a straggling
    task holding a stale pre-terminal count can overwrite an already-terminal
    marker back to ``dead_letter=False`` shortly after it parked. Both land in
    the same safe direction — parking is delayed by a round, never triggered
    early — and the straggler's own next round re-parks it. Qdrant offers no
    payload-level CAS to close either with, and trading a lost round for that
    machinery is not worth it.
    """
    settings = get_settings()
    limit = settings.vector_sync_max_index_failures

    previous = 0
    try:
        qdrant_client = await get_qdrant_client()
        # Fetch by deterministic point ID rather than via ``_dead_letter_filter``:
        # that filter matches terminal markers only, and a lookup by ID needs no
        # payload index at all.
        points = await qdrant_client.retrieve(
            collection_name=settings.get_collection_name(),
            ids=[_generate_dead_letter_id(doc_type, doc_id)],
            with_payload=True,
            with_vectors=False,
        )
        if points:
            payload = dict(points[0].payload or {})
            if payload.get("etag") == etag and payload.get("tiers_sig") == tiers_sig:
                stored = payload.get(ATTEMPTS_KEY)
                # A marker written before this field existed counts as one prior
                # attempt rather than zero — it only ever recorded a failure.
                previous = stored if isinstance(stored, int) else 1
    except Exception as e:
        logger.warning(
            "Index-failure lookup failed for %s_%s (%s); counting as the first",
            doc_type,
            doc_id,
            e,
        )

    attempts = previous + 1
    terminal = attempts >= limit
    await _upsert_marker(
        doc_id,
        doc_type,
        etag,
        tiers_sig,
        reason,
        terminal=terminal,
        attempts=attempts,
        file_path=file_path,
    )
    if terminal:
        logger.warning(
            "Dead-lettered %s_%s after %s consecutive index failures "
            "(reason=%s, etag=%s); it will not be re-queued until its content or "
            "the escalation-tier set changes",
            doc_type,
            doc_id,
            attempts,
            reason,
            etag,
        )
    else:
        logger.info(
            "Index failure %s/%s for %s_%s (reason=%s); will retry",
            attempts,
            limit,
            doc_type,
            doc_id,
            reason,
        )
    return terminal


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

    Clears BOTH kinds — a terminal marker and a soft counting one. Deleting by
    deterministic point ID rather than by ``_dead_letter_filter`` is what makes
    that true (the filter matches ``dead_letter=True`` only); it is also cheaper,
    and needs no payload index. This is what makes ``record_index_failure``'s
    count consecutive: one success resets the budget.

    Idempotent and fail-safe: deleting a non-existent marker is a Qdrant no-op,
    and an error is logged rather than raised so it never breaks the indexing
    path that calls it.
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()
        await qdrant_client.delete(
            collection_name=settings.get_collection_name(),
            points_selector=models.PointIdsList(
                points=[_generate_dead_letter_id(doc_type, doc_id)]
            ),
        )
        logger.debug("Cleared dead-letter marker for %s_%s", doc_type, doc_id)
    except Exception as e:
        logger.warning(
            "Failed to clear dead-letter marker for %s_%s: %s", doc_type, doc_id, e
        )
