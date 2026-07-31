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
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from qdrant_client.models import (
    Condition,
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchAny,
    MatchText,
    MatchValue,
    PayloadField,
    Range,
)

from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.placeholder import get_placeholder_filter

if TYPE_CHECKING:
    from nextcloud_mcp_server.vector.folder_ancestors import FileIdResolver

logger = logging.getLogger(__name__)

# Upper bound on the number of folder filters a single search may apply. Caps
# the width of the ``Filter(should=[...])`` OR-clause built from path_prefixes
# so no caller can degrade query latency with a huge folder list. Mirrored by
# the ``Field(max_length=...)`` on the MCP tool and the Astrolabe PHP cap.
MAX_PATH_PREFIXES = 20

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
_owners_cache: OrderedDict[str, tuple[float, AccessibleScope]] = OrderedDict()


def clear_accessible_owners_cache() -> None:
    """Drop all cached accessible-owners entries (used by tests)."""
    _owners_cache.clear()


class _SharingClientProtocol(Protocol):
    """Subset of SharingClient that this module actually uses."""

    async def list_shares(
        self, path: str | None = None, shared_with_me: bool = False
    ) -> list[dict[str, Any]]: ...


class AccessibleScope(NamedTuple):
    """What one user may read, as resolved from their incoming OCS shares.

    Attributes:
        owners: ``{user_id} ∪ {uid_owner of each incoming share}``. The owner
            UIDs whose content is a *candidate* for this user.
        share_root_ids: Nextcloud fileids of the shared items themselves (the
            OCS ``file_source`` of each incoming share), as strings. For a
            folder share this is the mount root, whose descendants all carry it
            in ``folder_ancestors``; for a single-file share it is that file's
            own id. Used to narrow the owner branch from "everything this owner
            has indexed" down to "the subtrees they actually shared".
    """

    owners: list[str]
    share_root_ids: list[str]


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

    Granularity: the owner list alone is *owner-level*, not *file-level* — one
    incoming share makes that owner's whole indexed corpus a Qdrant candidate.
    ``list_accessible_scope`` also returns the shared items' fileids so the
    search path can narrow the owner branch to the shared subtrees; see
    ``build_ownership_filter(shared_root_ids=...)``. Callers that only need the
    owner UIDs (doc-type discovery, context expansion) can keep using this.

    Sharing API failures are non-fatal — we degrade to ``[user_id]`` and log
    so a hiccup in OCS doesn't black-hole the user's own search.
    """
    return (await list_accessible_scope(sharing_client, user_id)).owners


async def list_accessible_scope(
    sharing_client: _SharingClientProtocol,
    user_id: str,
) -> AccessibleScope:
    """Resolve ``user_id``'s incoming shares into owner UIDs + shared fileids.

    One OCS round-trip serves both halves, cached together for
    ``_OWNERS_CACHE_TTL_SECONDS``. Failures are not cached and degrade to
    self-only scope (``owners=[user_id]``, no share roots), which is safe: the
    user still searches their own content, and verify-on-read remains the
    authority for anything that does surface.

    See ``list_accessible_owners`` for the OCS pagination caveat.
    """
    now = time.monotonic()
    cached = _owners_cache.get(user_id)
    if cached is not None and now - cached[0] < _OWNERS_CACHE_TTL_SECONDS:
        _owners_cache.move_to_end(user_id)  # mark as recently used (LRU)
        # Copy the lists so callers can't mutate the cached value.
        return AccessibleScope(list(cached[1].owners), list(cached[1].share_root_ids))

    owners: set[str] = {user_id}
    share_root_ids: set[str] = set()
    try:
        shares = await sharing_client.list_shares(shared_with_me=True)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning(
            "Sharing API unavailable; falling back to self-only owner filter "
            "for user %s (%s)",
            user_id,
            exc,
        )
        # Don't cache failures — retry on the next search.
        return AccessibleScope([user_id], [])

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
        # ``file_source`` is the fileid of the shared item itself (OCS also
        # exposes the same value as ``item_source``). Stringified to match the
        # ``folder_ancestors`` payload, which stores fileids as strings.
        root = share.get("file_source")
        if root is None:
            root = share.get("item_source")
        if isinstance(root, (int, str)) and str(root).strip():
            share_root_ids.add(str(root).strip())

    result = AccessibleScope(sorted(owners), sorted(share_root_ids))
    _owners_cache[user_id] = (now, result)
    # Promote to the most-recently-used end. This is a no-op for a brand-new
    # key (dict insertion already appends) but is needed when re-inserting an
    # existing key after its TTL expired.
    _owners_cache.move_to_end(user_id)
    while len(_owners_cache) > _OWNERS_CACHE_MAXSIZE:
        _owners_cache.popitem(last=False)  # evict the least-recently-used entry
    logger.debug(
        "Accessible scope for user %s: %d owner(s) (%d other), %d share root(s)",
        user_id,
        len(result.owners),
        len(result.owners) - 1,
        len(result.share_root_ids),
    )
    return AccessibleScope(list(result.owners), list(result.share_root_ids))


def build_ownership_filter(
    user_id: str,
    accessible_owners: list[str] | None = None,
    shared_root_ids: list[str] | None = None,
) -> Filter:
    """Build the Qdrant ``Filter`` constraining a search to readable points.

    Matches points whose ``owner_id`` is in ``accessible_owners`` (excluding
    self) OR whose ``user_id`` equals ``user_id`` OR whose ``acl_principals``
    contains ``user:<user_id>``. The ``user_id`` branch covers *all* of the
    caller's own content — both new points (where ``owner_id == user_id``) and
    legacy points indexed before ``owner_id`` existed — so self is intentionally
    NOT repeated in the ``owner_id`` branch. The ``acl_principals`` branch covers
    files that were indexed once and deduplicated across users (user-agnostic
    point IDs): such a point's ``user_id``/``owner_id`` are the first indexer's,
    so only the observed-access principal set surfaces it to other readers.

    Args:
        user_id: Querying user (matched by the ``user_id`` branch, which is the
            self-only default when ``accessible_owners`` is None).
        accessible_owners: Pre-computed list of owner UIDs the user has
            access to. When None, defaults to ``[user_id]`` (no shares
            expansion — used by callers that genuinely want self-only
            scope such as eviction sweeps).
        shared_root_ids: Fileids of the items those owners actually shared
            (``AccessibleScope.share_root_ids``). When given, the ``owner_id``
            branch is additionally constrained to points inside one of those
            subtrees, which is what stops one incoming share exposing an
            owner's entire corpus as candidates. When None/empty the branch
            keeps its historical owner-level width.

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
        # Observed-access branch: a file deduplicated across users carries one
        # user-agnostic point set whose ``user_id``/``owner_id`` are the first
        # indexer's. ``acl_principals`` lists every user whose scanner has seen
        # (hence can read) the file, so this branch surfaces a shared/group-folder
        # file to every reader even when they were not the indexer. Verify-on-read
        # (_verify_files) is the precise ACL gate on the returned candidates.
        FieldCondition(key="acl_principals", match=MatchAny(any=[f"user:{user_id}"])),
    ]
    if other_owners:
        owner_branch: Condition = FieldCondition(
            key="owner_id", match=MatchAny(any=other_owners)
        )
        roots = [rid for rid in (shared_root_ids or []) if rid and rid.strip()]
        if roots:
            # Narrow "everything this owner indexed" to "the subtrees they
            # actually shared with me". Without this, a single incoming share
            # admits the owner's whole corpus as candidates; verify-on-read then
            # rejects them one by one, burning the over-fetch budget and
            # silently shortening result pages. (Measured on one tenant: 47% of
            # the collection was admitted here and rejected downstream.)
            #
            # Three OR-ed ways to be inside a shared subtree:
            #   * folder_ancestors contains a shared folder's id — the normal
            #     case for a folder share (ancestors run parent → user root).
            #   * doc_id IS a shared id — a single-file share, and the shared
            #     folder's own point, neither of which lists itself as an
            #     ancestor.
            #   * folder_ancestors is empty/absent — fail open, because we have
            #     no containment signal to judge these on.
            #
            # SCOPE — that third branch is broader than "legacy points", and
            # deliberately so. ``folder_ancestors`` is only ever populated for
            # ``doc_type == "file"`` (vector/processor.py); every other doc type
            # is stamped with ``[]``, and Qdrant's ``IsEmpty`` matches an empty
            # array and an absent key alike. So the branch covers BOTH:
            #   - files predating ADR-033 Phase 3 (or whose ancestors failed to
            #     resolve) — an admin payload backfill converts these into the
            #     precise branches above, and
            #   - every non-file doc type (note, calendar, contact, deck_card,
            #     …), permanently, since they never get ancestors at all.
            # Those types therefore keep the old owner-level width: one share
            # from an owner still admits that owner's whole non-file corpus as
            # candidates. Narrowing them needs ancestors populated for those
            # types, which is a processor + re-index change, not a filter one.
            # Pinned by test_non_file_doc_types_are_not_narrowed so this stays a
            # known limitation rather than an accident.
            owner_branch = Filter(
                must=[
                    owner_branch,
                    Filter(
                        should=[
                            FieldCondition(
                                key=payload_keys.FOLDER_ANCESTORS,
                                match=MatchAny(any=roots),
                            ),
                            FieldCondition(key="doc_id", match=MatchAny(any=roots)),
                            IsEmptyCondition(
                                is_empty=PayloadField(key=payload_keys.FOLDER_ANCESTORS)
                            ),
                        ]
                    ),
                ]
            )
        conditions.insert(0, owner_branch)
    return Filter(should=conditions)


def normalize_path_prefixes(
    path_prefix: str | None = None,
    path_prefixes: Iterable[str] | None = None,
) -> list[str]:
    """Merge the legacy single ``path_prefix`` and list ``path_prefixes`` into
    one clean, de-duplicated, bounded list of folder filters.

    Blank/whitespace entries are dropped (an empty UI field must mean "no
    filter", not "match everything"), surrounding whitespace is stripped, and
    order is preserved while removing duplicates. Accepting both inputs keeps
    the pre-ADR-027-Phase-2 single-value contract working while callers migrate
    to the multi-folder list.

    The result is capped at ``MAX_PATH_PREFIXES`` so no caller — the MCP tool,
    the REST/viz endpoints, or a misbehaving client — can build an unbounded
    ``Filter(should=[...])`` OR-clause that would widen every Qdrant query.
    This is the single server-side enforcement point (the MCP tool also
    declares ``Field(max_length=...)`` for an earlier, clearer validation
    error, and the Astrolabe PHP controller caps before forwarding).

    Args:
        path_prefix: Legacy single folder filter (deprecated; folded into the
            returned list).
        path_prefixes: Zero or more folder filters.

    Returns:
        Ordered, de-duplicated list of non-empty folder filters, capped at
        ``MAX_PATH_PREFIXES`` (possibly empty).
    """
    raw: list[str] = []
    if path_prefix:
        raw.append(path_prefix)
    if path_prefixes:
        raw.extend(path_prefixes)

    # Two-pass on purpose: the ``if path_prefix:`` guard above is truthy for a
    # whitespace-only string like ``"   "``, so the strip-and-drop pass below is
    # what actually removes it — collecting first keeps the dedup order stable.
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in raw:
        stripped = value.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            cleaned.append(stripped)
    return cleaned[:MAX_PATH_PREFIXES]


def build_base_filter_conditions(
    user_id: str,
    accessible_owners: list[str] | None = None,
    doc_type: str | None = None,
    modified_after: int | None = None,
    modified_before: int | None = None,
    path_prefix: str | None = None,
    path_prefixes: Iterable[str] | None = None,
    path_prefix_folder_ids: list[str] | None = None,
    shared_root_ids: list[str] | None = None,
) -> list[Condition]:
    """Build the common ``must`` conditions shared by every search algorithm.

    This is the single place the structured-filter contract (ADR-027) lives, so
    both the BM25-hybrid (MCP tool) and dense-only (visualization/API) algorithms
    apply identical placeholder/ACL/doc_type/date filtering. Each algorithm wraps
    the returned list in ``Filter(must=...)`` and may append its own additive
    conditions afterward (e.g. the dense algorithm's opt-in ACL pre-filter).

    The conditions, in order:

    1. ``get_placeholder_filter()`` — exclude in-flight placeholder points.
    2. ``build_ownership_filter(...)`` — ACL-aware ``owner_id``/``user_id`` scope.
    3. ``doc_type`` exact match — only when ``doc_type`` is truthy.
    4. ``modified_at`` range — only when at least one bound is given.
    5. ``file_path`` text match — only when a path filter is given. One folder
       adds a single ``MatchText`` to ``must``; multiple folders are OR-ed via a
       nested ``Filter(should=[...])`` so a file under *any* selected folder
       matches.

    Args:
        user_id: Querying user.
        accessible_owners: Owner UIDs the user can read (see
            ``build_ownership_filter``). ``None`` ⇒ self-only.
        doc_type: Optional single document-type filter.
        modified_after: Inclusive lower bound on ``modified_at`` (Unix seconds).
        modified_before: Inclusive upper bound on ``modified_at`` (Unix seconds).
        path_prefix: Deprecated single folder/path filter; folded into
            ``path_prefixes``. Kept for backward compatibility.
        path_prefixes: Optional folder/path filters on the ``file_path`` payload
            field (ADR-027 Phase 2). Each is implemented with ``MatchText``
            against the text-indexed ``file_path`` and multiple folders are
            OR-ed together. ``file_path`` is only written for
            ``doc_type == "file"`` points, so any non-empty path filter
            implicitly restricts results to files. NOTE the match semantics
            differ by backend: server Qdrant tokenizes (AND-of-tokens, so
            ``"/Projects/Reports"`` matches files whose path contains both the
            ``Projects`` and ``Reports`` tokens), while the local/embedded
            qdrant-client matches by substring containment. Both serve folder
            scoping; neither is a strict left-anchored prefix.
        shared_root_ids: Fileids of the caller's incoming shares
            (``AccessibleScope.share_root_ids``), used to narrow the ACL
            owner branch to the shared subtrees. See ``build_ownership_filter``.

    Returns:
        A list of Qdrant ``Condition`` objects for a parent ``must`` clause.
    """
    conditions: list[Condition] = [
        get_placeholder_filter(),
        build_ownership_filter(user_id, accessible_owners, shared_root_ids),
    ]

    if doc_type:
        conditions.append(
            FieldCondition(key="doc_type", match=MatchValue(value=doc_type))
        )

    # ``Range`` treats ``None`` bounds as open-ended, so the same condition serves
    # after-only, before-only, and both-bounds queries. Appended only when at
    # least one bound is set so unfiltered searches add no condition.
    if modified_after is not None or modified_before is not None:
        conditions.append(
            FieldCondition(
                key="modified_at",
                range=Range(gte=modified_after, lte=modified_before),
            )
        )

    # Folder scoping (ADR-027 Phase 2 + ADR-033 Phase 3). Two branches are OR-ed:
    #
    #   * folder-ancestor containment — when the caller resolved the prefixes to
    #     folder fileids (``path_prefix_folder_ids``), a single
    #     ``MatchAny(folder_ancestors, folder_ids)`` gives a TRUE left-anchored
    #     containment that is user-agnostic (a shared folder's canonical fileid is
    #     identical for every reader) and evaluated inside HNSW traversal.
    #   * legacy ``MatchText(file_path)`` per folder — retained as a fallback for
    #     points that predate ``folder_ancestors`` (until an admin backfill
    #     populates them) and for prefixes that could not be resolved to a fileid.
    #
    # OR-ing keeps recall during migration (an un-backfilled point still matches
    # via file_path); once a collection is fully backfilled the folder-ancestor
    # branch is authoritative and the MatchText branch only adds the same-or-
    # looser matches. The whole disjunction is AND-ed against the other ``must``
    # conditions (ACL, doc_type, date).
    folders = normalize_path_prefixes(path_prefix, path_prefixes)
    folder_ids = [fid for fid in (path_prefix_folder_ids or []) if fid and fid.strip()]
    if folders or folder_ids:
        should: list[Condition] = []
        if folder_ids:
            should.append(
                FieldCondition(
                    key=payload_keys.FOLDER_ANCESTORS, match=MatchAny(any=folder_ids)
                )
            )
        should.extend(
            FieldCondition(key="file_path", match=MatchText(text=folder))
            for folder in folders
        )
        # A single branch attaches directly to ``must`` (the original Phase 2
        # shape for one folder, no resolved id); multiple branches OR under a
        # nested ``Filter(should=...)``.
        if len(should) == 1:
            conditions.append(should[0])
        elif should:
            conditions.append(Filter(should=should))

    return conditions


async def resolve_prefix_folder_ids(
    webdav: FileIdResolver,
    path_prefix: str | None = None,
    path_prefixes: Iterable[str] | None = None,
) -> list[str]:
    """Resolve folder path-prefixes to their canonical Nextcloud fileids.

    ADR-033 Phase 3: the folder-scope filter matches on ``folder_ancestors``
    (fileids), so a caller resolves the user's path prefixes to folder fileids
    once, *before* the query, and passes them to
    ``build_base_filter_conditions(path_prefix_folder_ids=...)``. A shared
    folder's fileid is identical for every user who mounts it, so the resolved id
    scopes correctly for owner and readers alike.

    Best-effort: a prefix that does not resolve (404, not a folder, transport
    error) is omitted — the query then falls back to that prefix's
    ``MatchText(file_path)`` branch. Returns the resolved fileids in prefix order.
    """
    folders = normalize_path_prefixes(path_prefix, path_prefixes)
    resolved: list[str] = []
    for folder in folders:
        try:
            fileid = await webdav.get_fileid(folder)
        except Exception as exc:  # noqa: BLE001 — best-effort; fall back to MatchText
            logger.debug(
                "Prefix folder-id resolve failed for %r (%s); using file_path match",
                folder,
                exc,
            )
            fileid = None
        if fileid:
            resolved.append(fileid)
    return resolved
