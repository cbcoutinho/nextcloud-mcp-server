"""Scanner task for vector database synchronization.

Periodically scans enabled users' content and queues changed documents for processing.
"""

import logging
import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, cast

import anyio
from anyio.abc import TaskStatus
from httpx import HTTPStatusError
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchAny,
    MatchValue,
    PayloadField,
    Record,
)

from nextcloud_mcp_server.capabilities import allowed_doc_types, is_doc_type_allowed
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.client.news import NewsItemType
from nextcloud_mcp_server.config import Settings, get_settings
from nextcloud_mcp_server.models.deck import DeckCard
from nextcloud_mcp_server.observability.metrics import (
    record_vector_sync_deletions_suppressed,
    record_vector_sync_scan,
)
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.server.tag_exclusion import (
    get_excluded_file_paths,
    is_path_excluded,
)
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.dead_letter import is_dead_lettered
from nextcloud_mcp_server.vector.mail_content import MAIL_SCAN_MAX_PER_MAILBOX
from nextcloud_mcp_server.vector.placeholder import (
    query_document_metadata,
    write_placeholder_point,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client
from nextcloud_mcp_server.vector.queue.ports import TaskProducer

if TYPE_CHECKING:
    from nextcloud_mcp_server.search.algorithms import NextcloudClientProtocol

from nextcloud_mcp_server.vector.sharing_state import (
    ACL_PRINCIPALS_KEY,
    claim_existing_index,
    reconcile_document_path,
    user_principal,
)

logger = logging.getLogger(__name__)


# Single source of truth for which doc_types this scanner indexes. The verifier
# registry in `search/verification.py` must cover every type listed here
# (enforced by `tests/unit/search/test_verification.py`). Add a verifier in the
# same PR that adds a new indexed doc_type, or accept ghost-record exposure for
# that type (see ADR-019).
INDEXED_DOC_TYPES: frozenset[str] = frozenset(
    {"note", "file", "deck_card", "news_item", "mail_message"}
)


# Page size for paginated deletion-tracking scrolls. Chosen to keep per-page
# memory bounded while making the round-trip count manageable in the typical
# < 100 k point per (user_id, doc_type) case. The previous single-page
# ``limit=10_000`` silently truncated deletion sets for any user past the
# cap, so anything indexed beyond the first 10 k was never reconciled.
#
# Intentionally larger than the ``batch_size = 256`` used by
# ``_backfill_doc_id_to_string`` in ``vector/qdrant_client.py``: this is a
# read-only scroll that just collects payloads (no write round-trip per
# point), so the per-page memory budget is the only relevant constraint.
# The 256 there is sized for read-write upsert batches where Qdrant
# accepts ~256-point chunks comfortably without timing out under load.
_DELETION_TRACKING_PAGE_SIZE: int = 1024


async def _scroll_all_points(
    qdrant_client: AsyncQdrantClient,
    *,
    collection_name: str,
    scroll_filter: Filter,
    payload_fields: list[str],
    page_size: int = _DELETION_TRACKING_PAGE_SIZE,
) -> list[Record]:
    """Scroll every point matching the filter, paginating until exhausted.

    Replaces the prior single-page ``limit=10_000`` calls that silently
    dropped points beyond the first page. Pagination follows Qdrant's
    documented contract: ``scroll`` returns ``(points, next_page_offset)``
    and ``next_page_offset`` is ``None`` once the cursor reaches the end.
    Errors propagate to the caller — the scanner's outer ``try`` already
    handles them by skipping the deletion-tracking pass for this scan
    (worse: extra-scan latency; never: bad data).
    """
    all_points: list[Record] = []
    offset = None
    while True:
        points, offset = await qdrant_client.scroll(
            collection_name=collection_name,
            scroll_filter=scroll_filter,
            with_payload=payload_fields,
            with_vectors=False,
            limit=page_size,
            offset=offset,
        )
        all_points.extend(points)
        if offset is None:
            break
    return all_points


@dataclass
class DocumentTask:
    """Document task for processing queue."""

    user_id: str
    doc_id: str  # Always str — see vector/qdrant_client.py keyword index
    doc_type: str  # "note", "file", "calendar"
    operation: str  # "index" or "delete"
    modified_at: int
    file_path: str | None = None  # File path for files (when doc_id is file_id)
    metadata: dict[str, int | str] | None = (
        None  # Additional metadata (e.g., board_id/stack_id for deck_card)
    )
    # Change-detection token (Nextcloud etag). Used as the external ingest
    # content_hash when present; the bus producer falls back to modified_at
    # when it is None (deletes, or sources whose etag isn't threaded). Harmless
    # in local mode — the in-process processor reads its own etag.
    etag: str | None = None
    # UID of the true owner of the indexed object, used by the search-time
    # ACL filter. None today (scanner always runs as the owner, so the
    # processor falls back to user_id), but settable so a future
    # shared-with-me crawl can pass through the actual owner without
    # reshaping the payload contract.
    owner_id: str | None = None
    # Per-document index mode: "hybrid" (dense + BM25 sparse, the default and what
    # every non-file producer emits) or "keyword" (BM25 sparse only). Set to
    # "keyword" for files discovered via the ``keyword-index`` tag so the
    # processor skips dense embedding for them. See payload_keys.INDEX_MODE_*.
    index_mode: str = payload_keys.INDEX_MODE_HYBRID
    # WebDAV getcontentlength at scan time, for files. Lets the processor apply
    # the oversize cap BEFORE downloading, so an over-cap document costs nothing
    # instead of being fetched into memory only to be rejected (a 531 MB PDF
    # OOMKilled a worker mid-download, before the cap was ever evaluated).
    # None = unknown (deletes, non-file types, webhook-produced tasks, and jobs
    # deferred by a pre-upgrade producer) -> the post-download guard still
    # applies. Defaulted so old in-flight job payloads deserialize unchanged.
    size_bytes: int | None = None


# Track documents potentially deleted (grace period before actual deletion)
# Format: {(user_id, doc_id, doc_type): first_missing_timestamp}. doc_type is
# part of the key so the same numeric id under different doc types (a note 42
# and a mail_message 42 for one user) tracks grace periods independently.
_potentially_deleted: dict[tuple[str, str, str], float] = {}

# Per-(user_id, index_mode) count of *consecutive* scan cycles whose tag
# discovery returned zero files for that mode while Qdrant still held indexed
# points for it. A single empty read from a flaky Nextcloud systemtag REPORT is
# byte-indistinguishable from a genuine "no tagged files", so a transient empty
# is treated as a failed read and that mode's deletions are suppressed until the
# streak reaches ``vector_sync_empty_discovery_delete_threshold`` (a *sustained*
# empty = a real mass-untag). The key is popped on any healthy read, so healthy
# users hold no entry. A dict (not a Counter) so it stays insertion-ordered for
# oldest-first eviction under the safety bound below.
#
# Concurrency: per-user scan tasks run concurrently (oauth_sync.user_scanner_task
# via ``tg.start_soon``), all mutating this one dict. It is safe because keys are
# ``(user_id, mode)`` (distinct per task, no cross-user collision) and the only
# mutator, ``_plan_file_deletions``, is synchronous — it has no ``await``, so a
# read-modify-write is never interleaved by the cooperative scheduler.
_empty_discovery_streak: dict[tuple[str, str], int] = {}

# Safety bound mirroring _CONSENT_BACKSTOP_MAX: cap the streak dict so heavy
# deprovisioned-user churn can't grow it without limit; evict oldest-first down
# to half capacity on overflow.
_EMPTY_DISCOVERY_STREAK_MAX = 50_000


def _bump_streak(streak_state: dict[tuple[str, str], int], key: tuple[str, str]) -> int:
    """Increment and return the consecutive-empty streak for ``key``, bounded.

    Evicts oldest-first down to half capacity on overflow (insertion-ordered
    dict), matching ``_mark_backstop_done``, so a bound hit only forgets the
    oldest streaks rather than clearing wholesale.
    """
    if key not in streak_state and len(streak_state) >= _EMPTY_DISCOVERY_STREAK_MAX:
        overage = len(streak_state) - _EMPTY_DISCOVERY_STREAK_MAX // 2
        logger.info(
            "empty-discovery streak tracking hit %d entries; evicting %d oldest",
            _EMPTY_DISCOVERY_STREAK_MAX,
            overage,
        )
        for stale_key in list(streak_state)[:overage]:
            del streak_state[stale_key]
    streak_state[key] = streak_state.get(key, 0) + 1
    return streak_state[key]


@dataclass
class _FileDeletionPlan:
    """Outcome of reconciling indexed file points against a scan's discovery.

    Attributes:
        to_delete: doc_ids to enqueue for deletion this cycle (grace elapsed).
        suppressed_by_mode: index_mode -> count of candidate deletions withheld
            because that mode's discovery was an implausible empty read.
        streaks: index_mode -> current consecutive-empty streak, for logging.
    """

    to_delete: list[str]
    suppressed_by_mode: dict[str, int]
    streaks: dict[str, int]


def _plan_file_deletions(
    *,
    user_id: str,
    indexed_by_mode: dict[str, set[str]],
    nextcloud_file_ids: set[str],
    discovered_by_mode: dict[str, int],
    attempted_modes: set[str],
    grace_state: dict[tuple[str, str, str], float],
    streak_state: dict[tuple[str, str], int],
    now: float,
    grace_period: float,
    empty_delete_threshold: int,
) -> _FileDeletionPlan:
    """Decide which indexed file points to delete, fail-safe against empty reads.

    A mode's discovery is *implausible* this cycle iff it was attempted, returned
    zero files, yet Qdrant still holds indexed points for it — the signature of a
    flaky/empty tag read (issue: blackbox-demo re-index flap). While a mode's
    consecutive-empty streak is below ``empty_delete_threshold`` its deletions are
    suppressed and its grace timers are left untouched (neither started nor
    advanced), so a transient empty deletes nothing. Once the streak reaches the
    threshold (a sustained empty = a genuine mass-untag) the mode's deletions
    proceed normally. Any healthy read (>0 discovered) pops the streak and
    restores normal grace-based deletion.

    A mode that was *not attempted* (files admin-disabled, or the keyword tag
    disabled) is an intentional zero — its streak is reset and its deletions
    proceed, preserving the existing disable-purge backstop. The discriminator is
    always "was discovery attempted", never "did it return 0".

    Pure except for the passed-in ``grace_state`` / ``streak_state`` dicts, which
    it mutates; it does no I/O. Callers own the enqueue, logging, and metrics.
    """
    suppressed_modes, streaks = _update_empty_discovery_streaks(
        user_id=user_id,
        indexed_by_mode=indexed_by_mode,
        discovered_by_mode=discovered_by_mode,
        attempted_modes=attempted_modes,
        streak_state=streak_state,
        empty_delete_threshold=empty_delete_threshold,
    )
    to_delete, suppressed_by_mode = _sweep_missing_files(
        user_id=user_id,
        indexed_by_mode=indexed_by_mode,
        nextcloud_file_ids=nextcloud_file_ids,
        suppressed_modes=suppressed_modes,
        grace_state=grace_state,
        now=now,
        grace_period=grace_period,
    )
    return _FileDeletionPlan(
        to_delete=to_delete,
        suppressed_by_mode=suppressed_by_mode,
        streaks=streaks,
    )


def _update_empty_discovery_streaks(
    *,
    user_id: str,
    indexed_by_mode: dict[str, set[str]],
    discovered_by_mode: dict[str, int],
    attempted_modes: set[str],
    streak_state: dict[tuple[str, str], int],
    empty_delete_threshold: int,
) -> tuple[set[str], dict[str, int]]:
    """Advance per-mode empty-discovery streaks; return (suppressed_modes, streaks).

    A mode is *implausible* iff it was attempted, discovered zero, yet still has
    indexed points — bump its streak and suppress deletions while the streak is
    below ``empty_delete_threshold``. Any other outcome (not attempted → an
    intentional zero; healthy read; nothing indexed) clears the streak.
    """
    suppressed_modes: set[str] = set()
    streaks: dict[str, int] = {}
    for mode in set(indexed_by_mode) | attempted_modes:
        streak_key = (user_id, mode)
        implausible = (
            mode in attempted_modes
            and discovered_by_mode.get(mode, 0) == 0
            and len(indexed_by_mode.get(mode, set())) > 0
        )
        if not implausible:
            streak_state.pop(streak_key, None)
            continue
        streak = _bump_streak(streak_state, streak_key)
        streaks[mode] = streak
        if streak < empty_delete_threshold:
            suppressed_modes.add(mode)
    return suppressed_modes, streaks


def _sweep_missing_files(
    *,
    user_id: str,
    indexed_by_mode: dict[str, set[str]],
    nextcloud_file_ids: set[str],
    suppressed_modes: set[str],
    grace_state: dict[tuple[str, str, str], float],
    now: float,
    grace_period: float,
) -> tuple[list[str], dict[str, int]]:
    """Grace-tracked deletion sweep; return (to_delete, suppressed_by_mode).

    For a suppressed mode, count the missing docs without touching their grace
    timers (so a transient empty neither starts nor advances a deletion). For an
    unsuppressed mode, a doc missing beyond ``grace_period`` is enqueued for
    deletion; a first miss just starts the grace clock.
    """
    to_delete: list[str] = []
    suppressed_by_mode: dict[str, int] = {}
    for mode, indexed_ids in indexed_by_mode.items():
        missing = indexed_ids - nextcloud_file_ids
        if mode in suppressed_modes:
            if missing:
                suppressed_by_mode[mode] = len(missing)
            continue
        for doc_id in missing:
            grace_key = (user_id, doc_id, "file")
            first_missing = grace_state.get(grace_key)
            if first_missing is None:
                # First time missing — start the grace period.
                grace_state[grace_key] = now
            elif now - first_missing >= grace_period:
                to_delete.append(doc_id)
                del grace_state[grace_key]
    return to_delete, suppressed_by_mode


def _record_suppressed_deletions(
    scan_id: str | int,
    plan: _FileDeletionPlan,
    indexed_by_mode: dict[str, set[str]],
    threshold: int,
) -> None:
    """Log + meter the per-mode file deletions the fail-safe gate withheld.

    Emits one WARNING and one ``record_vector_sync_deletions_suppressed`` counter
    increment per mode that actually suppressed at least one candidate this scan;
    modes with a zero count are skipped. Kept out of ``scan_user_documents`` so
    the metric/log wiring is unit-testable without driving the full async scan.
    """
    for mode, suppressed in plan.suppressed_by_mode.items():
        if not suppressed:
            continue
        logger.warning(
            "[SCAN-%s] Suppressed %d file deletion(s) for mode %s: tag "
            "discovery returned 0 but Qdrant holds %d indexed doc(s) — "
            "treating as a failed read (streak %d/%d)",
            scan_id,
            suppressed,
            mode,
            len(indexed_by_mode.get(mode, set())),
            plan.streaks.get(mode, 0),
            threshold,
        )
        record_vector_sync_deletions_suppressed(mode, suppressed)


def _indexed_files_scroll_filter(user_id: str) -> Filter:
    """Filter selecting a user's readable indexed file points for deletion tracking.

    Keyed on ``acl_principals`` (the observed-access set), **not** the immutable
    ``user_id`` indexer stamp. Chunk point IDs are user-agnostic, so a file shared
    across users (a team/group folder) has one point set stamped with only its
    *first* indexer's ``user_id`` plus an ``acl_principals`` set of every reader.
    ``release_document_for_user`` drops a leaving user's principal but never
    clears/reassigns ``user_id``. So keying deletion tracking on ``user_id`` meant
    the original indexer, once they lost access, re-selected the shared points
    every scan (their discovery is now empty), re-issued a release, and — because
    the release only touches ``acl_principals`` — never shrank the set: a
    perpetual no-op release loop (blackbox-demo team-folder removal, 2.7k
    releases/hr). Keying on the principal makes a release converge: once a user's
    principal is dropped, the next scan no longer selects the doc.

    Consequence (intentional): a "pure claimer" (in ``acl_principals`` but not the
    ``user_id``) is now tracked too, so it releases its own principal eagerly on
    the first scan after losing access, rather than relying on lazy verify-on-read
    eviction. Each reader manages exactly its own principal; the last release
    removes the points.

    Legacy points written before ``acl_principals`` existed carry no principal
    set, so the principal branch would never surface them (they'd orphan on file
    deletion). A second ``should`` branch keeps tracking those by ``user_id`` —
    but *only* when ``acl_principals`` is empty, so it can't reintroduce the
    original-indexer loop for modern points (which always carry the set). This
    mirrors ``release_document_for_user``'s own legacy ``user_id`` fallback.
    """
    return Filter(
        must=[FieldCondition(key="doc_type", match=MatchValue(value="file"))],
        should=[
            # Modern points: I am an observed reader of this shared document.
            FieldCondition(
                key=ACL_PRINCIPALS_KEY,
                match=MatchAny(any=[user_principal(user_id)]),
            ),
            # Legacy points (no acl_principals): fall back to the indexer stamp.
            Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    IsEmptyCondition(is_empty=PayloadField(key=ACL_PRINCIPALS_KEY)),
                ]
            ),
        ],
    )


async def _discover_tagged_files(
    nc_client: "NextcloudClientProtocol", settings: Settings
) -> list[dict]:
    """Discover tagged PDFs for both index modes, stamping ``_index_mode``.

    ``vector_sync_tag`` → hybrid (dense + BM25 sparse); ``vector_sync_keyword_tag``
    → keyword (BM25 sparse only). Hybrid wins precedence: a file carrying both tags
    is hybrid (a superset of keyword), so it is discovered once with
    ``_index_mode="hybrid"`` and its keyword listing is dropped. The keyword tag is
    only queried when ``vector_sync_keyword_tag`` is non-empty (empty = disabled),
    so single-tag deployments issue exactly one OCS Tags query as before.

    Each returned dict is a ``find_files_by_tag`` row (id/path/etag/...) plus an
    ``_index_mode`` key consumed by the enqueue loop.
    """
    hybrid_files = await nc_client.find_files_by_tag(
        settings.vector_sync_tag, mime_type_filter="application/pdf"
    )
    for f in hybrid_files:
        f["_index_mode"] = payload_keys.INDEX_MODE_HYBRID
        logger.debug(
            "Scanned file %s (ID: %s) carries the hybrid tag %r -> index_mode=%s",
            f.get("path"),
            f.get("id"),
            settings.vector_sync_tag,
            payload_keys.INDEX_MODE_HYBRID,
        )

    keyword_tag = settings.vector_sync_keyword_tag
    if not keyword_tag:
        logger.info(
            "Tagged-file discovery: %d hybrid (tag %r); keyword tag disabled",
            len(hybrid_files),
            settings.vector_sync_tag,
        )
        return hybrid_files

    hybrid_ids = {str(f["id"]) for f in hybrid_files}
    keyword_files = await nc_client.find_files_by_tag(
        keyword_tag, mime_type_filter="application/pdf"
    )
    # Hybrid precedence: drop keyword rows for files already tagged hybrid.
    extra_keyword_files = []
    for f in keyword_files:
        if str(f["id"]) in hybrid_ids:
            # File carries both tags: hybrid wins (superset of keyword).
            logger.debug(
                "Scanned file %s (ID: %s) carries both the hybrid tag %r and the "
                "keyword-only tag %r; hybrid precedence -> index_mode=%s",
                f.get("path"),
                f.get("id"),
                settings.vector_sync_tag,
                keyword_tag,
                payload_keys.INDEX_MODE_HYBRID,
            )
            continue
        f["_index_mode"] = payload_keys.INDEX_MODE_KEYWORD
        logger.debug(
            "Scanned file %s (ID: %s) carries the keyword-only tag %r -> index_mode=%s",
            f.get("path"),
            f.get("id"),
            keyword_tag,
            payload_keys.INDEX_MODE_KEYWORD,
        )
        extra_keyword_files.append(f)
    logger.info(
        "Tagged-file discovery: %d hybrid (tag %r), %d keyword-only (tag %r)",
        len(hybrid_files),
        settings.vector_sync_tag,
        len(extra_keyword_files),
        keyword_tag,
    )
    return hybrid_files + extra_keyword_files


async def get_last_indexed_timestamp(user_id: str) -> int | None:
    """Get the most recent indexed_at timestamp for user's notes in Qdrant.

    This timestamp can be used as pruneBefore parameter to optimize data transfer
    when fetching notes - only notes modified after this timestamp will be sent
    with full data.

    Args:
        user_id: User to query

    Returns:
        Unix timestamp of most recently indexed note, or None if no notes indexed yet
    """
    # TODO: This is O(N) over a user's indexed notes on every incremental
    # sync tick. Was accidentally bounded at 10 k before this PR (single-
    # page scroll silently truncated); paginating fixed correctness but
    # made the unbounded cost visible. Track the max ``indexed_at`` as
    # collection metadata or a dedicated sentinel point so this becomes
    # O(1). Out of scope for the current PR — see the chunk-context /
    # vector-sync follow-up tracker (referenced by the canonical TODO at
    # ``api/visualization.py``).
    try:
        qdrant_client = await get_qdrant_client()

        # Scroll across every indexed note for this user — paginated so users
        # with > 10 k indexed notes still produce a correct max (the prior
        # single-page ``limit=10_000`` would have silently undercounted).
        points = await _scroll_all_points(
            qdrant_client,
            collection_name=get_settings().get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value="note")),
                ]
            ),
            payload_fields=["indexed_at"],
        )

        num_points = len(points)
        logger.info("Found %s indexed notes in Qdrant for user %s", num_points, user_id)

        if points:
            timestamps = [
                point.payload.get("indexed_at", 0)
                for point in points
                if point.payload is not None
            ]
            max_timestamp = max(timestamps) if timestamps else 0
            logger.info(
                "Max indexed_at: %s, timestamps sample: %s",
                max_timestamp,
                timestamps[:3],
            )
            return int(max_timestamp) if max_timestamp > 0 else None

        logger.info("No indexed notes found for user %s", user_id)
        return None
    except Exception as e:
        logger.warning("Failed to get last indexed timestamp: %s", e)
        return None


async def scanner_task(
    send_stream: TaskProducer,
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    nc_client: NextcloudClient,
    user_id: str,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
):
    """
    Periodic scanner that detects changed documents for enabled user.

    For BasicAuth mode, scans a single user with credentials available at runtime.

    Args:
        send_stream: Stream to send changed documents to processors
        shutdown_event: Event signaling shutdown
        wake_event: Event to trigger immediate scan
        nc_client: Authenticated Nextcloud client
        user_id: User to scan
        task_status: Status object for signaling task readiness
    """
    logger.info("Scanner task started for user: %s", user_id)
    settings = get_settings()

    # Signal that the task has started and is ready
    task_status.started()

    async with send_stream:
        while not shutdown_event.is_set():
            try:
                # Scan user documents
                await scan_user_documents(
                    user_id=user_id,
                    send_stream=send_stream,
                    nc_client=nc_client,
                )

            except Exception as e:
                logger.error("Scanner error: %s", e)

            # Sleep until next interval or wake event
            try:
                with anyio.move_on_after(settings.vector_sync_scan_interval):
                    # Wait for wake event or shutdown (whichever comes first)
                    await wake_event.wait()
            except anyio.get_cancelled_exc_class():
                # Shutdown, exit loop
                break

    logger.info("Scanner task stopped - stream closed")


async def _get_enabled_apps_or_none(
    nc_client: NextcloudClient, user_id: str, scan_id: int
) -> set[str] | None:
    """Enabled-app id set for gating, or ``None`` when detection fails.

    ``None`` signals "couldn't determine" — callers must then scan every app
    (the prior behaviour), so a transient navigation-endpoint failure never
    silently halts indexing. The per-app 404 guards in ``scan_user_documents``
    remain the safety net for that fallback path.
    """
    try:
        return await nc_client.get_enabled_apps()
    except Exception as e:
        logger.warning(
            "[SCAN-%s] Could not determine enabled apps for %s (%s); scanning all apps",
            scan_id,
            user_id,
            e,
        )
        return None


def _app_enabled(app_id: str, enabled_apps: set[str] | None) -> bool:
    """Whether ``app_id`` should be scanned for the current user.

    ``enabled_apps is None`` means detection failed — every app is treated as
    enabled (the scan-all fallback) so a transient navigation-endpoint failure
    never silently halts indexing.
    """
    return enabled_apps is None or app_id in enabled_apps


def _should_scan(
    app_id: str,
    doc_type: str,
    enabled_apps: set[str] | None,
    allowed: frozenset[str] | None,
) -> bool:
    """Whether to scan ``app_id``: installed for the user AND admin-approved."""
    return _app_enabled(app_id, enabled_apps) and is_doc_type_allowed(doc_type, allowed)


# Text doc types whose deletion-tracking lives *inside* their scan_* function,
# so skipping that function (when admin-disabled) leaves indexed points with no
# grace-period backstop. Derived from INDEXED_DOC_TYPES so a newly-indexed type
# automatically gets the backstop. ``file`` is excluded: its scan path empties
# discovery and lets the existing reconcile loop purge on disable.
_TEXT_BACKSTOP_DOC_TYPES: tuple[str, ...] = tuple(sorted(INDEXED_DOC_TYPES - {"file"}))

# Per-process record of (user_id, doc_type) whose consent backstop deletes have
# already been enqueued, so a *standing* admin-disable doesn't re-flood the
# processor with idempotent deletes on every scan tick. An entry is cleared once
# the type is allowed again, so a later re-disable re-triggers the backstop.
# A dict (not a set) so it stays insertion-ordered for oldest-first eviction.
_consent_backstop_done: dict[tuple[str, str], None] = {}

# Safety bound on the tracking dict so a long-running multi-tenant process with
# heavy user churn (deprovisioned users leave stale entries) can't grow it
# without limit. At <= len(INDEXED_DOC_TYPES) entries per user this is generous;
# on overflow we evict the *oldest* entries down to half capacity (not a full
# clear) so the backstop re-fires for only those, avoiding a fleet-wide burst.
_CONSENT_BACKSTOP_MAX = 50_000


def _mark_backstop_done(key: tuple[str, str]) -> None:
    """Record a one-shot backstop marker, evicting oldest entries on overflow.

    Evicts oldest-first down to half capacity (insertion-ordered dict) rather
    than clearing wholesale, so a bound hit re-fires the backstop only for the
    oldest markers, not the whole fleet. A re-fire is idempotent regardless.
    """
    if len(_consent_backstop_done) >= _CONSENT_BACKSTOP_MAX:
        overage = len(_consent_backstop_done) - _CONSENT_BACKSTOP_MAX // 2
        logger.info(
            "consent backstop tracking hit %d entries; evicting %d oldest",
            _CONSENT_BACKSTOP_MAX,
            overage,
        )
        for stale_key in list(_consent_backstop_done)[:overage]:
            del _consent_backstop_done[stale_key]
    _consent_backstop_done[key] = None


async def _backstop_delete_doc_type(
    user_id: str,
    send_stream: TaskProducer,
    doc_type: str,
    qdrant_client: AsyncQdrantClient,
    collection: str,
    scan_id: int,
) -> int:
    """Enqueue delete tasks for every indexed point of one disabled doc_type.

    Returns the number of delete tasks enqueued.
    """
    points = await _scroll_all_points(
        qdrant_client,
        collection_name=collection,
        scroll_filter=Filter(
            must=[
                FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
            ]
        ),
        payload_fields=["doc_id"],
    )
    doc_ids = {
        str(p.payload["doc_id"])
        for p in points
        if p.payload is not None and "doc_id" in p.payload
    }
    if doc_ids:
        logger.info(
            "[SCAN-%s] %s disabled by admin for %s; enqueueing %d delete(s) (backstop)",
            scan_id,
            doc_type,
            user_id,
            len(doc_ids),
        )
    for doc_id in doc_ids:
        await send_stream.send(
            DocumentTask(
                user_id=user_id,
                doc_id=doc_id,
                doc_type=doc_type,
                operation="delete",
                modified_at=0,
            )
        )
    return len(doc_ids)


async def _enqueue_deletes_for_disabled_types(
    user_id: str,
    send_stream: TaskProducer,
    allowed: frozenset[str] | None,
    scan_id: int,
) -> int:
    """Enqueue delete tasks for indexed text-source points the admin disabled.

    Backstop for a failed eager purge: scrolls this user's indexed points for
    each admin-disallowed text doc_type and queues a delete. No-op when
    ``allowed`` is ``None`` (fail-open — never delete on a transient capability
    read failure). Returns the number of delete tasks enqueued.
    """
    if allowed is None:
        return 0

    # Re-enabled types: clear their one-shot marker so a later re-disable
    # re-triggers the backstop.
    for doc_type in _TEXT_BACKSTOP_DOC_TYPES:
        if doc_type in allowed:
            _consent_backstop_done.pop((user_id, doc_type), None)

    # Disabled types not yet backstopped this episode.
    disabled = [
        dt
        for dt in _TEXT_BACKSTOP_DOC_TYPES
        if dt not in allowed and (user_id, dt) not in _consent_backstop_done
    ]
    if not disabled:
        return 0

    qdrant_client = await get_qdrant_client()
    collection = get_settings().get_collection_name()
    queued = 0
    for doc_type in disabled:
        queued += await _backstop_delete_doc_type(
            user_id, send_stream, doc_type, qdrant_client, collection, scan_id
        )
        # Mark backstopped (even when nothing was found) so subsequent scans
        # don't re-scroll/re-enqueue for this disable episode.
        _mark_backstop_done((user_id, doc_type))
    return queued


async def scan_user_documents(
    user_id: str,
    send_stream: TaskProducer,
    nc_client: NextcloudClient,
    initial_sync: bool = False,
):
    """
    Scan a single user's documents and send changes to processor stream.

    Args:
        user_id: User to scan
        send_stream: Stream to send changed documents to processors
        nc_client: Authenticated Nextcloud client
        initial_sync: If True, send all documents (first-time sync)
    """

    scan_id = random.randint(1000, 9999)
    logger.info(
        "[SCAN-%s] Starting scan for user: %s, initial_sync=%s",
        scan_id,
        user_id,
        initial_sync,
    )

    with trace_operation(
        "vector_sync.scan_user_documents",
        attributes={
            "vector_sync.operation": "scan",
            "vector_sync.user_id": user_id,
            "vector_sync.initial_sync": initial_sync,
            "vector_sync.scan_id": scan_id,
        },
    ):
        # Calculate prune timestamp for optimized data transfer
        # Only notes modified after this will be sent with full data
        prune_before = (
            None if initial_sync else await get_last_indexed_timestamp(user_id)
        )
        if prune_before:
            logger.info(
                "[SCAN-%s] Using pruneBefore=%s to optimize data transfer",
                scan_id,
                prune_before,
            )

        # For deletion tracking, get all doc_ids in Qdrant (for incremental sync)
        # Note: We no longer bulk-query indexed_at, instead check per-document.
        # Hoisted to function scope so the file-scroll block below doesn't
        # depend on a name bound inside the notes-scroll block; future
        # refactors that add an early return between the two blocks would
        # otherwise hit an UnboundLocalError. get_qdrant_client is a
        # singleton call, so the cost is identical.
        qdrant_client = await get_qdrant_client() if not initial_sync else None
        indexed_doc_ids = set()
        if not initial_sync:
            # ``assert ... is not None`` would also narrow but raises an
            # opaque AssertionError under ``-O`` and at runtime — ``cast``
            # is the conventional zero-cost narrower for branches the type
            # checker can't infer from the surrounding ``if not
            # initial_sync`` (the ternary above ties the two together).
            qdrant_client = cast(AsyncQdrantClient, qdrant_client)
            points = await _scroll_all_points(
                qdrant_client,
                collection_name=get_settings().get_collection_name(),
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                        FieldCondition(key="doc_type", match=MatchValue(value="note")),
                    ]
                ),
                payload_fields=["doc_id"],
            )

            indexed_doc_ids = {
                str(point.payload["doc_id"])
                for point in points
                if point.payload is not None and "doc_id" in point.payload
            }

            logger.debug("Found %s indexed documents in Qdrant", len(indexed_doc_ids))

        # Determine which apps are enabled for this user so we skip polling
        # apps they lack — those polls 404 and flood tenant logs. ``None`` means
        # detection failed: fall back to scanning every app (prior behaviour).
        enabled_apps = await _get_enabled_apps_or_none(nc_client, user_id, scan_id)

        # Admin consent gate (Astrolabe): only index sources the admin has
        # approved for semantic search. ``None`` = no restriction (fail-open /
        # older Astrolabe), so a transient capabilities failure never silently
        # halts (or worse, mass-deletes) indexing. This is independent of
        # ``enabled_apps``, which reflects only what the user has installed.
        allowed = await allowed_doc_types(nc_client, user_id)

        # Notes (isolated so an uninstalled or disabled Notes app — whose API
        # returns 404 — cannot abort scanning of the other apps; this mirrors the
        # per-app try/except guards already wrapping files/news/deck below).
        settings = get_settings()
        grace_period = settings.vector_sync_scan_interval * 1.5
        current_time = time.time()
        queued = 0

        # Backstop purge for admin-disabled text sources. Their deletion-
        # tracking lives inside the scan_* function we skip below, so (unlike
        # files, whose discovery-empties-then-reconcile path purges on disable)
        # they'd linger if Astrolabe's eager purge failed. Enqueue deletes for
        # any indexed points of a now-disallowed type. Gated on a concrete
        # allow-set, so a fail-open None never triggers deletion.
        queued += await _enqueue_deletes_for_disabled_types(
            user_id, send_stream, allowed, scan_id
        )

        if _should_scan("notes", "note", enabled_apps, allowed):
            try:
                queued += await scan_notes(
                    user_id=user_id,
                    send_stream=send_stream,
                    nc_client=nc_client,
                    initial_sync=initial_sync,
                    scan_id=scan_id,
                    prune_before=prune_before,
                    indexed_doc_ids=indexed_doc_ids,
                    grace_period=grace_period,
                    current_time=current_time,
                )
            except HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.info(
                        "[SCAN-%s] Notes app unavailable for %s (HTTP 404); skipping notes",
                        scan_id,
                        user_id,
                    )
                else:
                    logger.warning("Failed to scan notes for %s: %s", user_id, e)
            except Exception as e:
                logger.warning("Failed to scan notes for %s: %s", user_id, e)
        else:
            logger.debug(
                "[SCAN-%s] Notes app not enabled for %s; skipping notes",
                scan_id,
                user_id,
            )

        if initial_sync:
            logger.info("Sent %s documents for initial sync: %s", queued, user_id)
            return

        # Scan tagged PDF files (after notes)
        # Get this user's readable indexed file points from Qdrant (for deletion
        # tracking), keyed on acl_principals (the observed-access set) rather than
        # the immutable user_id indexer stamp — see _indexed_files_scroll_filter
        # for why (blackbox-demo team-folder-removal release loop). Keying on the
        # principal makes a release converge and also tracks pure claimers, so a
        # reader who lost access releases its own principal on the next scan.
        indexed_by_mode: dict[str, set[str]] = {}
        if not initial_sync:
            assert qdrant_client is not None  # narrow for the type checker
            points = await _scroll_all_points(
                qdrant_client,
                collection_name=settings.get_collection_name(),
                scroll_filter=_indexed_files_scroll_filter(user_id),
                payload_fields=["doc_id", payload_keys.INDEX_MODE],
            )

            # Bucket indexed file points by index mode so the deletion fail-safe
            # can reason per mode (a flaky read can zero one tag but not the
            # other). Points written before INDEX_MODE existed carry no key and
            # were dense+sparse, so they default to hybrid — matching how the
            # modified-at gate below reads INDEX_MODE.
            for point in points:
                if point.payload is not None and "doc_id" in point.payload:
                    did = str(point.payload["doc_id"])
                    mode = point.payload.get(
                        payload_keys.INDEX_MODE, payload_keys.INDEX_MODE_HYBRID
                    )
                    indexed_by_mode.setdefault(mode, set()).add(did)

            logger.debug(
                "Found %s indexed files in Qdrant",
                sum(len(ids) for ids in indexed_by_mode.values()),
            )

        # Scan for tagged PDF files
        file_count = 0
        file_queued = 0
        nextcloud_file_ids = set()

        try:
            # Find tagged PDFs via the OCS Tags API. find_files_by_tag also
            # expands tagged directories into their PDF descendants (Depth:
            # infinity SEARCH), so a tag on a folder applies to every PDF beneath
            # it. Two tags feed one pipeline: ``vector_sync_tag`` →
            # hybrid (dense + sparse), ``vector_sync_keyword_tag`` → keyword
            # (sparse only). Each file dict is stamped with ``_index_mode`` so the
            # per-document processor knows which to apply; hybrid wins when a file
            # carries both (it is a superset of keyword). ``vector_sync_keyword_tag``
            # empty (default) disables the second tag entirely.
            settings = get_settings()
            if is_doc_type_allowed("file", allowed):
                tagged_files = await _discover_tagged_files(nc_client, settings)
            else:
                # Files disabled by admin: discover nothing so no new file is
                # indexed. The deletion-reconcile below then sees every indexed
                # file as "missing" and purges it after the grace period — the
                # backstop for the eager purge Astrolabe runs on disable.
                # Asymmetry (intentional): files purge up to 1.5x scan_interval
                # later than text types, which get immediate one-shot backstop
                # deletes via _enqueue_deletes_for_disabled_types.
                logger.debug(
                    "[SCAN-%s] Files disabled by admin for %s; skipping tagged-file discovery",
                    scan_id,
                    user_id,
                )
                tagged_files = []

            # Apply EXCLUDED_TAGS as defense-in-depth: a folder marked
            # off-limits via the exclusion tag must not be indexed even if
            # it (or an ancestor) also carries the include tag. Mirrors the
            # "exclusion wins" contract enforced by the MCP file tools.
            try:
                excluded_paths = await get_excluded_file_paths(nc_client.webdav)
            except Exception as e:
                logger.warning(
                    "[SCAN-%s] EXCLUDED_TAGS lookup failed (%s); "
                    "proceeding without exclusion filter",
                    scan_id,
                    e,
                )
                excluded_paths = set()
            if excluded_paths:
                before = len(tagged_files)
                tagged_files = [
                    f
                    for f in tagged_files
                    if not is_path_excluded(f.get("path", ""), excluded_paths)
                ]
                skipped = before - len(tagged_files)
                if skipped:
                    logger.info(
                        "[SCAN-%s] Skipped %d tagged file(s) under EXCLUDED_TAGS paths",
                        scan_id,
                        skipped,
                    )

            # Escalation-tier fingerprint for the dead-letter skip below, computed
            # once per scan. Lazy import: document_processors.__init__ pulls the
            # heavy parse stack (pymupdf/_isolation, Unix-only ``resource``; #877),
            # which the scanner (API role) must not load at module import.
            from nextcloud_mcp_server.document_processors.escalation import (  # noqa: PLC0415
                escalation_tiers_signature,
            )

            tiers_sig = escalation_tiers_signature(get_settings())

            for file_info in tagged_files:
                # Files are already filtered by MIME type in find_files_by_tag()
                file_count += 1
                # Normalize file ID to str — Qdrant doc_id payload is keyword-indexed
                # and producers across doc_types must agree on a single type.
                file_id = str(file_info["id"])
                file_path = file_info["path"]  # Keep path for logging
                # Which index mode this file's tag selected (hybrid default;
                # keyword for keyword-index-tagged files). Threaded into the dedup
                # claim and the DocumentTask so the processor embeds accordingly.
                index_mode = file_info.get(
                    "_index_mode", payload_keys.INDEX_MODE_HYBRID
                )
                nextcloud_file_ids.add(file_id)

                # Use last_modified timestamp if available, otherwise use current time
                modified_at = file_info.get("last_modified_timestamp", int(time.time()))
                if isinstance(file_info.get("last_modified"), str):
                    # Parse RFC 2822 date format if needed
                    try:
                        dt = parsedate_to_datetime(file_info["last_modified"])
                        modified_at = int(dt.timestamp())
                    except (ValueError, KeyError):
                        pass

                # Tenant-wide content dedup (Layer 1 / observed-access ACL): if
                # this exact file content (fileid + etag) is already indexed under
                # the current embedding model by ANY user in the tenant, skip
                # re-parsing/re-embedding and just record that this user can read
                # it. Eliminates the per-user reprocessing ping-pong that arises
                # because chunk point IDs are user-agnostic (note 386945 #5).
                etag = str(file_info.get("etag") or "")
                # getcontentlength is already requested and parsed by the WebDAV
                # discovery calls, so carrying it costs no extra request. The
                # parser yields 0 when the property is absent; treat that as
                # "unknown" so the processor falls back to the post-download
                # guard rather than gating on a bogus zero.
                size_bytes = file_info.get("size") or None
                if etag and await claim_existing_index(
                    file_id,
                    "file",
                    etag,
                    user_id,
                    index_mode=index_mode,
                    current_path=file_path,
                ):
                    _potentially_deleted.pop((user_id, file_id, "file"), None)
                    logger.debug(
                        "Dedup: file %s (ID: %s) already indexed in tenant; "
                        "granted access to %s without reprocessing",
                        file_path,
                        file_id,
                        user_id,
                    )
                    continue

                # Tenant-wide dead-letter skip: a document that terminally failed
                # parsing (no escalation tier) is recorded user-agnostically, so
                # EVERY user's scan skips re-queuing it until its content (etag) or
                # the escalation-tier set (tiers_sig, e.g. OCR enabled) changes.
                # Unlike the per-user placeholder "failed" mark this is not
                # defeated by a file shared across users -- whose single
                # user-agnostic placeholder's user_id is overwritten by the last
                # scanner, so every other user re-queued it on a loop.
                if etag and await is_dead_lettered(file_id, "file", etag, tiers_sig):
                    _potentially_deleted.pop((user_id, file_id, "file"), None)
                    logger.debug(
                        "Skipping dead-lettered file %s (ID: %s) until content/"
                        "tier change",
                        file_path,
                        file_id,
                    )
                    continue

                if initial_sync:
                    # Send everything on first sync - write placeholder first
                    await write_placeholder_point(
                        doc_id=file_id,
                        doc_type="file",
                        user_id=user_id,
                        modified_at=modified_at,
                        etag=etag,
                        file_path=file_path,
                    )
                    await send_stream.send(
                        DocumentTask(
                            user_id=user_id,
                            doc_id=file_id,
                            doc_type="file",
                            operation="index",
                            modified_at=modified_at,
                            file_path=file_path,
                            etag=etag,
                            index_mode=index_mode,
                            size_bytes=size_bytes,
                        )
                    )
                    file_queued += 1
                else:
                    # Incremental sync: check if file exists and compare modified_at
                    # If file reappeared, remove from potentially_deleted
                    file_key = (user_id, file_id, "file")
                    if file_key in _potentially_deleted:
                        logger.debug(
                            "File %s (ID: %s) reappeared, removing from deletion grace period",
                            file_path,
                            file_id,
                        )
                        del _potentially_deleted[file_key]

                    # Query Qdrant for existing entry (placeholder or real)
                    existing_metadata = await query_document_metadata(
                        doc_id=file_id, doc_type="file", user_id=user_id
                    )

                    # Send if never indexed or modified since last index
                    # Compare against stored modified_at (not indexed_at!)
                    needs_indexing = False
                    if existing_metadata is None:
                        # Never seen before
                        needs_indexing = True
                    elif existing_metadata.get("modified_at", 0) < modified_at:
                        # File modified since last indexing
                        needs_indexing = True
                    elif existing_metadata.get("is_placeholder", False):
                        # Placeholder exists - check its status / staleness.
                        queued_at = existing_metadata.get("queued_at", 0)
                        placeholder_age = time.time() - queued_at
                        stale_threshold = get_settings().vector_sync_scan_interval * 5
                        if existing_metadata.get("status") == "failed":
                            # A permanent parse failure (e.g. an isolated-worker
                            # OOM/timeout on a pathological PDF). Don't keep
                            # re-queuing an unchanged file that will just fail
                            # again -- the modified_at branch above still retries
                            # it once the file actually changes.
                            logger.debug(
                                "Skipping file %s (ID: %s): previous parse failed permanently",
                                file_path,
                                file_id,
                            )
                        elif placeholder_age > stale_threshold:
                            # Only requeue if placeholder is older than 5x scan
                            # interval (large PDFs can take minutes to process).
                            logger.debug(
                                "Found stale placeholder for file %s (ID: %s) (age=%ss), requeuing",
                                file_path,
                                file_id,
                                format(placeholder_age, ".1f"),
                            )
                            needs_indexing = True
                        else:
                            logger.debug(
                                "Skipping file %s (ID: %s) with recent placeholder (age=%ss < %ss)",
                                file_path,
                                file_id,
                                format(placeholder_age, ".1f"),
                                format(stale_threshold, ".1f"),
                            )
                    elif (
                        # Default hybrid: points indexed before INDEX_MODE existed
                        # carry no key and were dense+sparse, so they read as
                        # hybrid and don't spuriously reindex under a hybrid scan.
                        existing_metadata.get(
                            payload_keys.INDEX_MODE, payload_keys.INDEX_MODE_HYBRID
                        )
                        != index_mode
                    ):
                        # Real point whose index mode changed at unchanged content
                        # (a retag). In practice this is the keyword→hybrid upgrade:
                        # the modified_at gate above is stable, and the dedup claim
                        # can't reuse sparse-only points for a hybrid request, so
                        # reprocess to add the dense vector. (The hybrid→keyword
                        # downgrade is absorbed by the dedup no-downgrade rule and
                        # never reaches here.)
                        logger.info(
                            "File %s (ID: %s) index mode changed to %s; reindexing",
                            file_path,
                            file_id,
                            index_mode,
                        )
                        needs_indexing = True

                    if needs_indexing:
                        # Write placeholder before queuing
                        await write_placeholder_point(
                            doc_id=file_id,
                            doc_type="file",
                            user_id=user_id,
                            modified_at=modified_at,
                            etag=etag,
                            file_path=file_path,
                        )
                        await send_stream.send(
                            DocumentTask(
                                user_id=user_id,
                                doc_id=file_id,
                                doc_type="file",
                                operation="index",
                                modified_at=modified_at,
                                file_path=file_path,
                                etag=etag,
                                index_mode=index_mode,
                                size_bytes=size_bytes,
                            )
                        )
                        file_queued += 1
                    elif existing_metadata is not None and not existing_metadata.get(
                        "is_placeholder", False
                    ):
                        # Reached only on the rename-with-stable-mtime path: a
                        # fresh modified_at would have set needs_indexing, and an
                        # etag dedup hit would have continued above -- so here the
                        # content wasn't re-queued (modified_at stable) yet the
                        # stored path may be stale from a rename/move (the fileid
                        # is unchanged). Refresh path/title without re-embedding;
                        # reconcile_document_path no-ops when the path matches.
                        # Skip placeholders: reconcile only touches real chunks, so
                        # a not-yet-indexed file would just incur a 0-point
                        # set_payload (the real index writes the current path).
                        try:
                            # existing_metadata is user_id-scoped to this caller
                            # (query_document_metadata filters on user_id), so this
                            # path only ever fires for the point's own indexer —
                            # never the thrash source. Pass the owner-gate params
                            # (ADR-033 Phase 1) for consistency with the dedup-path
                            # caller; a no-op today since owner_id == user_id here.
                            await reconcile_document_path(
                                file_id,
                                "file",
                                existing_metadata.get("file_path"),
                                file_path,
                                caller_user_id=user_id,
                                owner_id=existing_metadata.get("owner_id"),
                            )
                        except Exception as exc:  # noqa: BLE001 — non-fatal
                            logger.warning(
                                "Path reconcile failed for file %s (ID: %s) (%s); "
                                "next scan retries",
                                file_path,
                                file_id,
                                exc,
                            )

            logger.info(
                "[SCAN-%s] Found %s tagged PDFs for %s", scan_id, file_count, user_id
            )
            record_vector_sync_scan(file_count)

            # Check for deleted files (not initial sync). Fail-safe: an empty
            # tag-discovery read (a flaky Nextcloud systemtag REPORT returning an
            # empty 207) must not be mistaken for "all files gone" and delete the
            # corpus. _plan_file_deletions gates deletions per index mode on a
            # consecutive-empty streak; see its docstring.
            if not initial_sync:
                # Which index modes did we actually attempt to discover this
                # cycle? Only an *attempted* mode that came back empty is
                # suspicious — an unattempted mode (files admin-disabled, or the
                # keyword tag disabled) is an intentional zero and must still
                # purge (the existing disable backstop).
                attempted_modes: set[str] = set()
                if is_doc_type_allowed("file", allowed):
                    attempted_modes.add(payload_keys.INDEX_MODE_HYBRID)
                    if settings.vector_sync_keyword_tag:
                        attempted_modes.add(payload_keys.INDEX_MODE_KEYWORD)

                discovered_by_mode: dict[str, int] = {}
                for f in tagged_files:
                    m = f.get("_index_mode", payload_keys.INDEX_MODE_HYBRID)
                    discovered_by_mode[m] = discovered_by_mode.get(m, 0) + 1

                plan = _plan_file_deletions(
                    user_id=user_id,
                    indexed_by_mode=indexed_by_mode,
                    nextcloud_file_ids=nextcloud_file_ids,
                    discovered_by_mode=discovered_by_mode,
                    attempted_modes=attempted_modes,
                    grace_state=_potentially_deleted,
                    streak_state=_empty_discovery_streak,
                    now=current_time,
                    grace_period=grace_period,
                    empty_delete_threshold=(
                        settings.vector_sync_empty_discovery_delete_threshold
                    ),
                )

                for file_id in plan.to_delete:
                    logger.info(
                        "File ID %s missing beyond %ss grace period, sending deletion",
                        file_id,
                        format(grace_period, ".1f"),
                    )
                    await send_stream.send(
                        DocumentTask(
                            user_id=user_id,
                            doc_id=file_id,
                            doc_type="file",
                            operation="delete",
                            modified_at=0,
                        )
                    )
                    file_queued += 1

                _record_suppressed_deletions(
                    scan_id,
                    plan,
                    indexed_by_mode,
                    settings.vector_sync_empty_discovery_delete_threshold,
                )

        except Exception as e:
            logger.warning("Failed to scan tagged files for %s: %s", user_id, e)

        queued += file_queued

        # Scan News items (starred + unread)
        news_queued = 0
        if _should_scan("news", "news_item", enabled_apps, allowed):
            try:
                news_queued = await scan_news_items(
                    user_id=user_id,
                    send_stream=send_stream,
                    nc_client=nc_client,
                    initial_sync=initial_sync,
                    scan_id=scan_id,
                )
                queued += news_queued
            except Exception as e:
                logger.warning("Failed to scan news items for %s: %s", user_id, e)
        else:
            logger.debug(
                "[SCAN-%s] News app not enabled for %s; skipping news items",
                scan_id,
                user_id,
            )

        # Scan Deck cards
        deck_queued = 0
        if _should_scan("deck", "deck_card", enabled_apps, allowed):
            try:
                deck_queued = await scan_deck_cards(
                    user_id=user_id,
                    send_stream=send_stream,
                    nc_client=nc_client,
                    initial_sync=initial_sync,
                    scan_id=scan_id,
                )
                queued += deck_queued
            except Exception as e:
                logger.warning("Failed to scan deck cards for %s: %s", user_id, e)
        else:
            logger.debug(
                "[SCAN-%s] Deck app not enabled for %s; skipping deck cards",
                scan_id,
                user_id,
            )

        # Scan Mail messages (newest per mailbox)
        mail_queued = 0
        if _should_scan("mail", "mail_message", enabled_apps, allowed):
            try:
                mail_queued = await scan_mail_messages(
                    user_id=user_id,
                    send_stream=send_stream,
                    nc_client=nc_client,
                    initial_sync=initial_sync,
                    scan_id=scan_id,
                )
                queued += mail_queued
            except Exception as e:
                logger.warning("Failed to scan mail messages for %s: %s", user_id, e)
        else:
            logger.debug(
                "[SCAN-%s] Mail app not enabled for %s; skipping mail messages",
                scan_id,
                user_id,
            )

        if queued > 0:
            logger.info(
                "Sent %s documents (%s files, %s news items, %s deck cards, "
                "%s mail messages) for incremental sync: %s",
                queued,
                file_queued,
                news_queued,
                deck_queued,
                mail_queued,
                user_id,
            )
        else:
            logger.debug("No changes detected for %s", user_id)


async def scan_notes(
    user_id: str,
    send_stream: TaskProducer,
    nc_client: NextcloudClient,
    initial_sync: bool,
    scan_id: int,
    prune_before: int | None,
    indexed_doc_ids: set[str],
    grace_period: float,
    current_time: float,
) -> int:
    """Scan a user's Notes and queue changed notes for indexing.

    Extracted into its own function (like scan_news_items / scan_deck_cards) so a
    failure here -- e.g. the Notes API returning 404 because the app is not
    installed -- propagates to the caller's per-app guard instead of aborting the
    whole user scan. The deletion-tracking pass runs only after the Notes fetch
    succeeds, so a failed fetch never mass-deletes a user's indexed notes.

    Returns:
        Number of notes queued for processing (index + delete operations).
    """
    # Stream notes from Nextcloud and process immediately
    note_count = 0
    queued = 0
    nextcloud_doc_ids: set[str] = set()

    async for note in nc_client.notes.get_all_notes(prune_before=prune_before):
        note_count += 1
        doc_id = str(note["id"])
        nextcloud_doc_ids.add(doc_id)
        modified_at = note.get("modified", 0)

        if initial_sync:
            # Send everything on first sync - write placeholder first
            await write_placeholder_point(
                doc_id=doc_id,
                doc_type="note",
                user_id=user_id,
                modified_at=modified_at,
                etag=note.get("etag", ""),
            )
            await send_stream.send(
                DocumentTask(
                    user_id=user_id,
                    doc_id=doc_id,
                    doc_type="note",
                    operation="index",
                    modified_at=modified_at,
                )
            )
            queued += 1
        else:
            # Incremental sync: check if document exists and compare modified_at
            # If document reappeared, remove from potentially_deleted
            doc_key = (user_id, doc_id, "note")
            if doc_key in _potentially_deleted:
                logger.debug(
                    "Document %s reappeared, removing from deletion grace period",
                    doc_id,
                )
                del _potentially_deleted[doc_key]

            # Query Qdrant for existing entry (placeholder or real)
            existing_metadata = await query_document_metadata(
                doc_id=doc_id, doc_type="note", user_id=user_id
            )

            # Send if never indexed or modified since last index
            # Compare against stored modified_at (not indexed_at!)
            needs_indexing = False
            if existing_metadata is None:
                # Never seen before
                needs_indexing = True
            elif existing_metadata.get("modified_at", 0) < modified_at:
                # Document modified since last indexing
                needs_indexing = True
            elif existing_metadata.get("is_placeholder", False):
                # Placeholder exists - check if it's stale (processing may have failed)
                # Only requeue if placeholder is older than 5x scan interval
                # (Large PDFs can take 3-4 minutes to process)
                queued_at = existing_metadata.get("queued_at", 0)
                placeholder_age = time.time() - queued_at
                stale_threshold = get_settings().vector_sync_scan_interval * 5
                if placeholder_age > stale_threshold:
                    logger.debug(
                        "Found stale placeholder for note %s (age=%ss), requeuing",
                        doc_id,
                        format(placeholder_age, ".1f"),
                    )
                    needs_indexing = True
                else:
                    logger.debug(
                        "Skipping note %s with recent placeholder (age=%ss < %ss)",
                        doc_id,
                        format(placeholder_age, ".1f"),
                        format(stale_threshold, ".1f"),
                    )

            if needs_indexing:
                # Write placeholder before queuing
                await write_placeholder_point(
                    doc_id=doc_id,
                    doc_type="note",
                    user_id=user_id,
                    modified_at=modified_at,
                    etag=note.get("etag", ""),
                )
                await send_stream.send(
                    DocumentTask(
                        user_id=user_id,
                        doc_id=doc_id,
                        doc_type="note",
                        operation="index",
                        modified_at=modified_at,
                    )
                )
                queued += 1

    # Log and record metrics after streaming
    logger.info("[SCAN-%s] Found %s notes for %s", scan_id, note_count, user_id)
    record_vector_sync_scan(note_count)

    if initial_sync:
        return queued

    # Check for deleted documents (in Qdrant but not in Nextcloud)
    # Use grace period: only delete after 2 consecutive scans confirm absence
    for doc_id in indexed_doc_ids:
        if doc_id not in nextcloud_doc_ids:
            doc_key = (user_id, doc_id, "note")

            if doc_key in _potentially_deleted:
                # Already marked as potentially deleted, check if grace period elapsed
                first_missing_time = _potentially_deleted[doc_key]
                time_missing = current_time - first_missing_time

                if time_missing >= grace_period:
                    # Grace period elapsed, send for deletion
                    logger.info(
                        "Document %s missing for %ss (>%ss grace period), sending deletion",
                        doc_id,
                        format(time_missing, ".1f"),
                        format(grace_period, ".1f"),
                    )
                    await send_stream.send(
                        DocumentTask(
                            user_id=user_id,
                            doc_id=doc_id,
                            doc_type="note",
                            operation="delete",
                            modified_at=0,
                        )
                    )
                    queued += 1
                    # Remove from tracking after sending deletion
                    del _potentially_deleted[doc_key]
                else:
                    logger.debug(
                        "Document %s still missing (%ss/%ss grace period)",
                        doc_id,
                        format(time_missing, ".1f"),
                        format(grace_period, ".1f"),
                    )
            else:
                # First time missing, add to grace period tracking
                logger.debug(
                    "Document %s missing for first time, starting grace period",
                    doc_id,
                )
                _potentially_deleted[doc_key] = current_time

    return queued


async def scan_news_items(
    user_id: str,
    send_stream: TaskProducer,
    nc_client: NextcloudClient,
    initial_sync: bool,
    scan_id: int,
) -> int:
    """
    Scan user's News items and queue changed items for indexing.

    Indexes all items from the user's feeds. The News app's auto-purge
    feature (default: 200 items per feed) naturally limits the total
    number of items, making explicit filtering unnecessary.

    Args:
        user_id: User to scan
        send_stream: Stream to send changed documents to processors
        nc_client: Authenticated Nextcloud client
        initial_sync: If True, send all documents (first-time sync)
        scan_id: Scan identifier for logging

    Returns:
        Number of items queued for processing
    """
    settings = get_settings()
    queued = 0

    # Get indexed news item IDs from Qdrant (for deletion tracking)
    indexed_item_ids: set[str] = set()
    if not initial_sync:
        qdrant_client = await get_qdrant_client()
        points = await _scroll_all_points(
            qdrant_client,
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value="news_item")),
                ]
            ),
            payload_fields=["doc_id"],
        )
        indexed_item_ids = {
            str(point.payload["doc_id"])
            for point in points
            if point.payload is not None and "doc_id" in point.payload
        }
        logger.debug("Found %s indexed news items in Qdrant", len(indexed_item_ids))

    # Fetch all items (News app caps at ~200 per feed via auto-purge)
    all_items = await nc_client.news.get_items(
        batch_size=-1,
        type_=NewsItemType.ALL,
        get_read=True,
    )
    logger.debug("[SCAN-%s] Found %s news items", scan_id, len(all_items))

    item_count = len(all_items)
    nextcloud_item_ids: set[str] = set()

    for item in all_items:
        doc_id = str(item["id"])
        nextcloud_item_ids.add(doc_id)

        # Use lastModified timestamp (microseconds in News API)
        modified_at = item.get("lastModified", 0)
        # Convert to seconds if needed (News API uses microseconds)
        if modified_at > 10000000000:  # > year 2286 in seconds
            modified_at = modified_at // 1000000

        if initial_sync:
            # Send everything on first sync - write placeholder first
            await write_placeholder_point(
                doc_id=doc_id,
                doc_type="news_item",
                user_id=user_id,
                modified_at=modified_at,
            )
            await send_stream.send(
                DocumentTask(
                    user_id=user_id,
                    doc_id=doc_id,
                    doc_type="news_item",
                    operation="index",
                    modified_at=modified_at,
                )
            )
            queued += 1
        else:
            # Incremental sync: check if item exists and compare modified_at
            doc_key = (user_id, doc_id, "news_item")
            if doc_key in _potentially_deleted:
                logger.debug(
                    "News item %s reappeared, removing from deletion grace period",
                    doc_id,
                )
                del _potentially_deleted[doc_key]

            # Query Qdrant for existing entry
            existing_metadata = await query_document_metadata(
                doc_id=doc_id, doc_type="news_item", user_id=user_id
            )

            needs_indexing = False
            if existing_metadata is None:
                needs_indexing = True
            elif existing_metadata.get("modified_at", 0) < modified_at:
                needs_indexing = True
            elif existing_metadata.get("is_placeholder", False):
                queued_at = existing_metadata.get("queued_at", 0)
                placeholder_age = time.time() - queued_at
                stale_threshold = settings.vector_sync_scan_interval * 5
                if placeholder_age > stale_threshold:
                    logger.debug(
                        "Found stale placeholder for news item %s (age=%ss), requeuing",
                        doc_id,
                        format(placeholder_age, ".1f"),
                    )
                    needs_indexing = True

            if needs_indexing:
                await write_placeholder_point(
                    doc_id=doc_id,
                    doc_type="news_item",
                    user_id=user_id,
                    modified_at=modified_at,
                )
                await send_stream.send(
                    DocumentTask(
                        user_id=user_id,
                        doc_id=doc_id,
                        doc_type="news_item",
                        operation="index",
                        modified_at=modified_at,
                    )
                )
                queued += 1

    logger.info(
        "[SCAN-%s] Found %s news items (starred+unread) for %s",
        scan_id,
        item_count,
        user_id,
    )
    record_vector_sync_scan(item_count)

    # Check for deleted items (not initial sync)
    # Items become "deleted" when they are no longer starred AND become read
    if not initial_sync:
        grace_period = settings.vector_sync_scan_interval * 1.5
        current_time = time.time()

        for doc_id in indexed_item_ids:
            if doc_id not in nextcloud_item_ids:
                doc_key = (user_id, doc_id, "news_item")

                if doc_key in _potentially_deleted:
                    first_missing_time = _potentially_deleted[doc_key]
                    time_missing = current_time - first_missing_time

                    if time_missing >= grace_period:
                        logger.info(
                            "News item %s missing for %ss (>%ss grace period), sending deletion",
                            doc_id,
                            format(time_missing, ".1f"),
                            format(grace_period, ".1f"),
                        )
                        await send_stream.send(
                            DocumentTask(
                                user_id=user_id,
                                doc_id=doc_id,
                                doc_type="news_item",
                                operation="delete",
                                modified_at=0,
                            )
                        )
                        queued += 1
                        del _potentially_deleted[doc_key]
                else:
                    logger.debug(
                        "News item %s missing for first time, starting grace period",
                        doc_id,
                    )
                    _potentially_deleted[doc_key] = current_time

    return queued


# Newest-N messages indexed per mailbox (= the Mail OCS per-request maximum;
# imported from mail_content so the scanner index window and the search-time
# verifier presence window stay identical). Older messages age out of the index
# as newer ones arrive — the deletion-tracking pass below evicts them, the same
# way it handles actually-deleted messages. Going beyond 100 would require
# cursor pagination, so the value is fixed rather than configurable.
#
# Per-process record of (user_id, mailbox_id) for which the newest-N cap has
# already been logged, so the "older mail not indexed" notice is emitted once at
# info level (discoverable) rather than on every scan tick (which would flood
# multi-tenant logs). Insertion-ordered dict + bounded eviction (mirrors
# _consent_backstop_done) so a long-running multi-tenant process can't leak it.
_mail_cap_logged: dict[tuple[str, int], None] = {}
_MAIL_CAP_LOGGED_MAX = 50_000


def _mark_mail_cap_logged(key: tuple[str, int]) -> bool:
    """Record a one-shot cap-log marker; return True if this is the first time.

    Evicts oldest-first to half capacity on overflow (a re-log after eviction is
    a harmless info line), so the dedup set stays bounded.
    """
    if key in _mail_cap_logged:
        return False
    if len(_mail_cap_logged) >= _MAIL_CAP_LOGGED_MAX:
        overage = len(_mail_cap_logged) - _MAIL_CAP_LOGGED_MAX // 2
        for stale_key in list(_mail_cap_logged)[:overage]:
            del _mail_cap_logged[stale_key]
    _mail_cap_logged[key] = None
    return True


async def scan_mail_messages(
    user_id: str,
    send_stream: TaskProducer,
    nc_client: NextcloudClient,
    initial_sync: bool,
    scan_id: int,
) -> int:
    """
    Scan a user's Mail messages and queue changed messages for indexing.

    Enumerates accounts → mailboxes → newest ``MAIL_SCAN_MAX_PER_MAILBOX``
    messages per mailbox. Email is immutable, so a message's ``dateInt`` (sent
    timestamp) is used as the change-detection ``modified_at`` — a message is
    indexed once and not re-sent. Messages that drop out of the newest-N window
    (or are deleted) are evicted via the deletion-tracking pass, keeping the
    index bounded to recent mail.

    The MCP server never speaks IMAP: listing reads the Mail app's DB-cached
    envelopes, and the body fetch (in the processor) goes through the Mail app's
    OCS API, which handles IMAP server-side.

    Args:
        user_id: User to scan
        send_stream: Stream to send changed documents to processors
        nc_client: Authenticated Nextcloud client
        initial_sync: If True, send all documents (first-time sync)
        scan_id: Scan identifier for logging

    Returns:
        Number of messages queued for processing
    """
    settings = get_settings()
    queued = 0

    # Get indexed mail message IDs from Qdrant (for deletion tracking)
    indexed_message_ids: set[str] = set()
    if not initial_sync:
        qdrant_client = await get_qdrant_client()
        points = await _scroll_all_points(
            qdrant_client,
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(
                        key="doc_type", match=MatchValue(value="mail_message")
                    ),
                ]
            ),
            payload_fields=["doc_id"],
        )
        indexed_message_ids = {
            str(point.payload["doc_id"])
            for point in points
            if point.payload is not None and "doc_id" in point.payload
        }
        logger.debug(
            "Found %s indexed mail messages in Qdrant", len(indexed_message_ids)
        )

    # Enumerate accounts → mailboxes → newest-N messages.
    accounts = await nc_client.mail.list_accounts()
    nextcloud_message_ids: set[str] = set()
    message_count = 0

    for account in accounts:
        account_id = account.get("id")
        if account_id is None:
            continue
        try:
            mailboxes = await nc_client.mail.get_mailboxes(account_id)
        except Exception as e:
            logger.warning(
                "[SCAN-%s] Failed to list mailboxes for account %s: %s",
                scan_id,
                account_id,
                e,
            )
            continue

        for mailbox in mailboxes:
            mailbox_id = mailbox.get("databaseId")
            if mailbox_id is None:
                continue
            try:
                messages = await nc_client.mail.list_messages(
                    mailbox_id, limit=MAIL_SCAN_MAX_PER_MAILBOX
                )
            except Exception as e:
                logger.warning(
                    "[SCAN-%s] Failed to list messages for mailbox %s: %s",
                    scan_id,
                    mailbox_id,
                    e,
                )
                continue

            if len(messages) >= MAIL_SCAN_MAX_PER_MAILBOX and _mark_mail_cap_logged(
                (user_id, mailbox_id)
            ):
                logger.info(
                    "[SCAN-%s] Mailbox %s contains more than %s messages; only "
                    "the newest %s are indexed (cursor pagination not yet "
                    "implemented)",
                    scan_id,
                    mailbox_id,
                    MAIL_SCAN_MAX_PER_MAILBOX,
                    MAIL_SCAN_MAX_PER_MAILBOX,
                )

            for message in messages:
                msg_db_id = message.get("databaseId")
                if msg_db_id is None:
                    continue
                doc_id = str(msg_db_id)
                nextcloud_message_ids.add(doc_id)
                message_count += 1

                modified_at = message.get("dateInt", 0) or 0
                task_metadata: dict[str, int | str] = {
                    "account_id": account_id,
                    "mailbox_id": mailbox_id,
                }

                if initial_sync:
                    await write_placeholder_point(
                        doc_id=doc_id,
                        doc_type="mail_message",
                        user_id=user_id,
                        modified_at=modified_at,
                    )
                    await send_stream.send(
                        DocumentTask(
                            user_id=user_id,
                            doc_id=doc_id,
                            doc_type="mail_message",
                            operation="index",
                            modified_at=modified_at,
                            metadata=task_metadata,
                        )
                    )
                    queued += 1
                else:
                    doc_key = (user_id, doc_id, "mail_message")
                    if doc_key in _potentially_deleted:
                        logger.debug(
                            "Mail message %s reappeared, removing from deletion "
                            "grace period",
                            doc_id,
                        )
                        del _potentially_deleted[doc_key]

                    existing_metadata = await query_document_metadata(
                        doc_id=doc_id, doc_type="mail_message", user_id=user_id
                    )

                    needs_indexing = False
                    if existing_metadata is None:
                        needs_indexing = True
                    elif existing_metadata.get("modified_at", 0) < modified_at:
                        needs_indexing = True
                    elif existing_metadata.get("status") == "failed":
                        # A permanent processing failure — don't re-queue an
                        # unchanged message that will just fail again; the
                        # modified_at branch above retries once it changes
                        # (mirrors the file scanner's failed-placeholder guard).
                        logger.debug(
                            "Skipping mail message %s: previous processing "
                            "failed permanently",
                            doc_id,
                        )
                    elif existing_metadata.get("is_placeholder", False):
                        queued_at = existing_metadata.get("queued_at", 0)
                        placeholder_age = time.time() - queued_at
                        stale_threshold = settings.vector_sync_scan_interval * 5
                        if placeholder_age > stale_threshold:
                            logger.debug(
                                "Found stale placeholder for mail message %s "
                                "(age=%ss), requeuing",
                                doc_id,
                                format(placeholder_age, ".1f"),
                            )
                            needs_indexing = True

                    if needs_indexing:
                        await write_placeholder_point(
                            doc_id=doc_id,
                            doc_type="mail_message",
                            user_id=user_id,
                            modified_at=modified_at,
                        )
                        await send_stream.send(
                            DocumentTask(
                                user_id=user_id,
                                doc_id=doc_id,
                                doc_type="mail_message",
                                operation="index",
                                modified_at=modified_at,
                                metadata=task_metadata,
                            )
                        )
                        queued += 1

    logger.info(
        "[SCAN-%s] Found %s mail messages for %s",
        scan_id,
        message_count,
        user_id,
    )
    record_vector_sync_scan(message_count)

    # Check for deleted / aged-out messages (not initial sync)
    if not initial_sync:
        grace_period = settings.vector_sync_scan_interval * 1.5
        current_time = time.time()

        for doc_id in indexed_message_ids:
            if doc_id not in nextcloud_message_ids:
                doc_key = (user_id, doc_id, "mail_message")

                if doc_key in _potentially_deleted:
                    first_missing_time = _potentially_deleted[doc_key]
                    time_missing = current_time - first_missing_time

                    if time_missing >= grace_period:
                        logger.info(
                            "Mail message %s missing for %ss (>%ss grace period), "
                            "sending deletion",
                            doc_id,
                            format(time_missing, ".1f"),
                            format(grace_period, ".1f"),
                        )
                        await send_stream.send(
                            DocumentTask(
                                user_id=user_id,
                                doc_id=doc_id,
                                doc_type="mail_message",
                                operation="delete",
                                modified_at=0,
                            )
                        )
                        queued += 1
                        del _potentially_deleted[doc_key]
                else:
                    logger.debug(
                        "Mail message %s missing for first time, starting grace period",
                        doc_id,
                    )
                    _potentially_deleted[doc_key] = current_time

    return queued


async def scan_deck_cards(
    user_id: str,
    send_stream: TaskProducer,
    nc_client: NextcloudClient,
    initial_sync: bool,
    scan_id: int,
) -> int:
    """
    Scan user's Deck cards and queue changed cards for indexing.

    Indexes cards from all non-archived boards and stacks.

    Args:
        user_id: User to scan
        send_stream: Stream to send changed documents to processors
        nc_client: Authenticated Nextcloud client
        initial_sync: If True, send all documents (first-time sync)
        scan_id: Scan identifier for logging

    Returns:
        Number of cards queued for processing
    """
    settings = get_settings()
    queued = 0

    # Get indexed deck card IDs from Qdrant (for deletion tracking)
    indexed_card_ids: set[str] = set()
    if not initial_sync:
        qdrant_client = await get_qdrant_client()
        points = await _scroll_all_points(
            qdrant_client,
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value="deck_card")),
                ]
            ),
            payload_fields=["doc_id"],
        )
        indexed_card_ids = {
            str(point.payload["doc_id"])
            for point in points
            if point.payload is not None and "doc_id" in point.payload
        }
        logger.debug("Found %s indexed deck cards in Qdrant", len(indexed_card_ids))

    # Fetch all boards
    boards = await nc_client.deck.get_boards()
    logger.debug("[SCAN-%s] Found %s deck boards", scan_id, len(boards))

    card_count = 0
    nextcloud_card_ids: set[str] = set()

    # Iterate through boards
    for board in boards:
        # Skip archived boards
        if board.archived:
            continue

        # Skip deleted boards (soft delete: deletedAt > 0)
        if board.deletedAt > 0:
            logger.debug("[SCAN-%s] Skipping deleted board %s", scan_id, board.id)
            continue

        # Get stacks for this board
        stacks = await nc_client.deck.get_stacks(board.id)

        # Iterate through stacks
        for stack in stacks:
            # Skip if stack has no cards
            if not stack.cards:
                continue

            # Iterate through cards in stack. get_stacks() always yields full
            # DeckCard objects; the DeckCardSummary projection only happens in
            # the tool layer (server/deck.py), never on freshly-fetched stacks.
            for card in cast(list[DeckCard], stack.cards):
                # Skip archived cards
                if card.archived:
                    continue

                card_count += 1
                doc_id = str(card.id)
                nextcloud_card_ids.add(doc_id)

                # Use lastModified timestamp if available
                modified_at = card.lastModified or 0

                if initial_sync:
                    # Send everything on first sync - write placeholder first
                    await write_placeholder_point(
                        doc_id=doc_id,
                        doc_type="deck_card",
                        user_id=user_id,
                        modified_at=modified_at,
                    )
                    await send_stream.send(
                        DocumentTask(
                            user_id=user_id,
                            doc_id=doc_id,
                            doc_type="deck_card",
                            operation="index",
                            modified_at=modified_at,
                            metadata={"board_id": board.id, "stack_id": stack.id},
                        )
                    )
                    queued += 1
                else:
                    # Incremental sync: check if card exists and compare modified_at
                    doc_key = (user_id, doc_id, "deck_card")
                    if doc_key in _potentially_deleted:
                        logger.debug(
                            "Deck card %s reappeared, removing from deletion grace period",
                            doc_id,
                        )
                        del _potentially_deleted[doc_key]

                    # Query Qdrant for existing entry
                    existing_metadata = await query_document_metadata(
                        doc_id=doc_id, doc_type="deck_card", user_id=user_id
                    )

                    needs_indexing = False
                    if existing_metadata is None:
                        needs_indexing = True
                    elif existing_metadata.get("modified_at", 0) < modified_at:
                        needs_indexing = True
                    elif existing_metadata.get("is_placeholder", False):
                        queued_at = existing_metadata.get("queued_at", 0)
                        placeholder_age = time.time() - queued_at
                        stale_threshold = settings.vector_sync_scan_interval * 5
                        if placeholder_age > stale_threshold:
                            logger.debug(
                                "Found stale placeholder for deck card %s (age=%ss), requeuing",
                                doc_id,
                                format(placeholder_age, ".1f"),
                            )
                            needs_indexing = True

                    if needs_indexing:
                        await write_placeholder_point(
                            doc_id=doc_id,
                            doc_type="deck_card",
                            user_id=user_id,
                            modified_at=modified_at,
                        )
                        await send_stream.send(
                            DocumentTask(
                                user_id=user_id,
                                doc_id=doc_id,
                                doc_type="deck_card",
                                operation="index",
                                modified_at=modified_at,
                                metadata={"board_id": board.id, "stack_id": stack.id},
                            )
                        )
                        queued += 1

    logger.info(
        "[SCAN-%s] Found %s deck cards (non-archived) for %s",
        scan_id,
        card_count,
        user_id,
    )
    record_vector_sync_scan(card_count)

    # Check for deleted cards (not initial sync)
    if not initial_sync:
        grace_period = settings.vector_sync_scan_interval * 1.5
        current_time = time.time()

        for doc_id in indexed_card_ids:
            if doc_id not in nextcloud_card_ids:
                doc_key = (user_id, doc_id, "deck_card")

                if doc_key in _potentially_deleted:
                    first_missing_time = _potentially_deleted[doc_key]
                    time_missing = current_time - first_missing_time

                    if time_missing >= grace_period:
                        logger.info(
                            "Deck card %s missing for %ss (>%ss grace period), sending deletion",
                            doc_id,
                            format(time_missing, ".1f"),
                            format(grace_period, ".1f"),
                        )
                        await send_stream.send(
                            DocumentTask(
                                user_id=user_id,
                                doc_id=doc_id,
                                doc_type="deck_card",
                                operation="delete",
                                modified_at=0,
                            )
                        )
                        queued += 1
                        del _potentially_deleted[doc_key]
                else:
                    logger.debug(
                        "Deck card %s missing for first time, starting grace period",
                        doc_id,
                    )
                    _potentially_deleted[doc_key] = current_time

    return queued
