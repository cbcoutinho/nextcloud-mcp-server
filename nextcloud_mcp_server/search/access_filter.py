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
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

logger = logging.getLogger(__name__)


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

    Sharing API failures are non-fatal — we degrade to ``[user_id]`` and log
    so a hiccup in OCS doesn't black-hole the user's own search.
    """
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
        return [user_id]

    for share in shares:
        # OCS returns the share owner under `uid_owner` (the file owner,
        # not the share recipient). Some Nextcloud versions also surface
        # `owner` as a fallback display field — we tolerate both.
        owner = share.get("uid_owner") or share.get("owner")
        if isinstance(owner, str) and owner:
            owners.add(owner)

    logger.debug("Accessible owners for user %s: %d entries", user_id, len(owners))
    return list(owners)


def build_ownership_filter(
    user_id: str, accessible_owners: list[str] | None = None
) -> Filter:
    """Build the Qdrant ``Filter`` constraining a search to readable points.

    Matches points whose ``owner_id`` is in ``accessible_owners`` OR whose
    legacy ``user_id`` equals ``user_id``. The legacy branch keeps points
    indexed before this change reachable until they're re-indexed.

    Args:
        user_id: Querying user (used for the legacy ``user_id`` fallback
            and as the only-self default when ``accessible_owners`` is None).
        accessible_owners: Pre-computed list of owner UIDs the user has
            access to. When None, defaults to ``[user_id]`` (no shares
            expansion — used by callers that genuinely want self-only
            scope such as eviction sweeps).

    Returns:
        A Qdrant ``Filter`` ready to be nested under a parent ``must`` clause.
    """
    owners = accessible_owners if accessible_owners is not None else [user_id]
    return Filter(
        should=[
            FieldCondition(key="owner_id", match=MatchAny(any=owners)),
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
        ]
    )
