"""ACL-aware ownership filter for semantic / BM25 search.

The vector store payload carries an ``owner_id`` field — the UID of the user
who owns the underlying Nextcloud document. At query time, a user should
be able to find every document whose owner has shared it (directly or via
group / link) with them, without re-indexing.

This module turns "who can user X read?" into a Qdrant filter:
``owner_id IN accessible_owners`` where ``accessible_owners`` is
``{X} ∪ {owners of files / objects shared with X}``.

A second OR-branch matches the legacy ``user_id`` field so points indexed
before this change (which carry only ``user_id``) continue to be findable
by their original indexer. New points carry both fields.

Operator note (existing data): a Qdrant ``owner_id`` field condition matches
nothing on points that lack the field, so documents indexed *before* this
change never surface to share recipients — only to their original indexer via
the legacy ``user_id`` branch. ACL-aware search is therefore effectively a
no-op for pre-existing data until each owner's scanner re-indexes it. Trigger a
re-index after deploying this feature if it should apply to already-indexed
content immediately.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Protocol

from qdrant_client.models import Condition, FieldCondition, Filter, MatchAny, MatchValue

logger = logging.getLogger(__name__)

# Short-lived per-user cache for the OCS shares lookup, which otherwise runs on
# every search/viz request. Trades up to this many seconds of share-visibility
# staleness (a freshly-granted share is searchable a little late) for avoiding
# an OCS round-trip per query. Safe: verify-on-read still gates each result
# against Nextcloud, so a revoked share is caught there regardless of this cache.
_OWNERS_CACHE_TTL_SECONDS = 30.0
# Cap the number of cached users so the process-global cache can't grow
# unboundedly in a long-running multi-user deployment (one entry per active
# user, never evicted otherwise). LRU eviction by insertion/access order via
# OrderedDict; the cap is generous relative to any realistic concurrent-user
# count, so steady state is effectively all-hit.
_OWNERS_CACHE_MAXSIZE = 1024
_owners_cache: OrderedDict[str, tuple[float, list[str]]] = OrderedDict()


def clear_accessible_owners_cache() -> None:
    """Drop all cached accessible-owners entries (used by tests)."""
    _owners_cache.clear()


class _SharingClientProtocol(Protocol):
    """Subset of SharingClient that this module actually uses."""

    async def list_shares(
        self, path: str | None = None, shared_with_me: bool = False
    ) -> list[dict[str, Any]]: ...


async def list_accessible_owners(
    sharing_client: _SharingClientProtocol,
    user_id: str,
) -> list[str]:
    """Return every owner UID whose content `user_id` should be able to search.

    The set is ``{user_id} ∪ {uid_owner of each share with shared_with_me=True}``.
    Duplicates are removed; ordering is not significant (Qdrant ``MatchAny``
    treats the list as a set).

    Results are cached per user for ``_OWNERS_CACHE_TTL_SECONDS`` to keep the
    OCS round-trip off the search hot path. Failures are not cached.

    Note: ``list_shares(shared_with_me=True)`` returns whatever the OCS endpoint
    yields in a single page (SharingClient does not paginate today). A user with
    more incoming shares than the OCS page size could have some owners omitted;
    if that becomes real, add pagination to SharingClient.

    Granularity / over-fetch limitation (TODO, finer-grained filtering): this
    expansion is *owner-level*, not *file-level*. If a prolific content creator
    shares a single item with the querying user, that owner's whole indexed
    corpus becomes a Qdrant candidate set for the querier even though only the
    shared item is accessible. Verify-on-read correctly drops the inaccessible
    "ghost" candidates, but because there is no second Qdrant pass to replenish,
    a ``limit=N`` search can return fewer than N results when the over-fetch
    buffer (2× in nc_semantic_search / viz_routes) is dominated by ghosts. A
    per-file ownership index would remove this tension and is the natural
    starting point for future work (intentionally out of scope here).

    Sharing API failures are non-fatal — we degrade to ``[user_id]`` and log
    so a hiccup in OCS doesn't black-hole the user's own search.
    """
    now = time.monotonic()
    cached = _owners_cache.get(user_id)
    if cached is not None and now - cached[0] < _OWNERS_CACHE_TTL_SECONDS:
        _owners_cache.move_to_end(user_id)  # mark as recently used (LRU)
        return list(cached[1])  # copy so callers can't mutate the cached value

    owners: set[str] = {user_id}
    try:
        shares = await sharing_client.list_shares(shared_with_me=True)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning(
            "Sharing API unavailable; falling back to self-only owner filter "
            "for user %s (%s)",
            user_id,
            exc,
        )
        return [user_id]  # don't cache failures — retry on the next search

    for share in shares:
        # OCS returns the share owner under `uid_owner` (the file owner,
        # not the share recipient). Some Nextcloud versions also surface
        # `owner` as a fallback display field — we tolerate both. The intent is
        # "absent, not empty": a missing/blank `uid_owner` falls through to
        # `owner`, and a non-string or empty result skips the (malformed) share.
        owner = share.get("uid_owner") or share.get("owner") or None
        if not isinstance(owner, str) or not owner:
            continue
        owners.add(owner)

    result = list(owners)
    _owners_cache[user_id] = (now, result)
    # Promote to the most-recently-used end. This is a no-op for a brand-new
    # key (dict insertion already appends) but is needed when re-inserting an
    # existing key after its TTL expired.
    _owners_cache.move_to_end(user_id)
    while len(_owners_cache) > _OWNERS_CACHE_MAXSIZE:
        _owners_cache.popitem(last=False)  # evict the least-recently-used entry
    logger.debug(
        "Accessible owners for user %s: %d entries (%d other owner(s))",
        user_id,
        len(result),
        len(result) - 1,
    )
    return list(result)


def build_ownership_filter(
    user_id: str, accessible_owners: list[str] | None = None
) -> Filter:
    """Build the Qdrant ``Filter`` constraining a search to readable points.

    Matches points whose ``owner_id`` is in ``accessible_owners`` (excluding
    self) OR whose ``user_id`` equals ``user_id``. The ``user_id`` branch covers
    *all* of the caller's own content — both new points (where
    ``owner_id == user_id``) and legacy points indexed before ``owner_id``
    existed — so self is intentionally NOT repeated in the ``owner_id`` branch.

    Args:
        user_id: Querying user (matched by the ``user_id`` branch, which is the
            self-only default when ``accessible_owners`` is None).
        accessible_owners: Pre-computed list of owner UIDs the user has
            access to. When None, defaults to ``[user_id]`` (no shares
            expansion — used by callers that genuinely want self-only
            scope such as eviction sweeps).

    Returns:
        A Qdrant ``Filter`` ready to be nested under a parent ``must`` clause.
    """
    owners = accessible_owners if accessible_owners is not None else [user_id]
    # The ``user_id`` branch is always present and already covers self-owned
    # content (new + legacy). The ``owner_id`` branch is added only for OTHER
    # owners (share senders) — listing self there too would overlap the
    # ``user_id`` branch for no benefit. When there are no other owners the
    # ``owner_id`` branch is omitted entirely, so we never depend on
    # ``MatchAny(any=[])`` matching nothing (not a documented Qdrant guarantee).
    other_owners = [owner for owner in owners if owner != user_id]
    conditions: list[Condition] = [
        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
    ]
    if other_owners:
        conditions.insert(
            0, FieldCondition(key="owner_id", match=MatchAny(any=other_owners))
        )
    return Filter(should=conditions)
