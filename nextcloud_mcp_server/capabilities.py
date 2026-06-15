"""Reads admin-approved searchable sources from the Astrolabe capability.

The Astrolabe Nextcloud app advertises, per user, which content sources an
admin has approved for semantic search, under
``capabilities.astrolabe.semantic_search.enabled_doc_types`` on the OCS
capabilities endpoint (``/ocs/v2.php/cloud/capabilities``). This is the single
source of truth for admin consent: the search layer filters results to these
doc types, and the indexing layer (scanner + webhook ingest) skips everything
else (the hard data-at-rest guarantee is the eager purge Astrolabe triggers on
disable; see ``vector.purge``).

Fail-open for *availability*: if the capability block is absent (an older
Astrolabe that predates this feature) or the OCS call fails, ``allowed_doc_types``
returns ``None`` meaning "no restriction", so search keeps working. ``None`` is
distinct from an empty set, which means "the admin disabled every source".
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Short-lived per-user cache for the OCS capabilities lookup. Admin consent
# changes rarely, but search/scan paths consult it frequently, so trade a little
# staleness for keeping the OCS round-trip off the hot path. Mirrors the
# list_accessible_owners cache in search/access_filter.py.
_CACHE_TTL_SECONDS = 30.0
_CACHE_MAXSIZE = 1024
# user_id -> (monotonic_ts, frozenset[doc_type] | None). None = no restriction.
_cache: OrderedDict[str, tuple[float, frozenset[str] | None]] = OrderedDict()


class _CapabilitiesClientProtocol(Protocol):
    async def capabilities(self) -> Any: ...


def _parse_enabled_doc_types(payload: Any) -> frozenset[str] | None:
    """Extract ``enabled_doc_types`` from an OCS capabilities payload.

    Returns ``None`` when the ``astrolabe.semantic_search`` block is absent or
    malformed (treated as "no restriction"). Returns a frozenset (possibly
    empty) when the block is present and well-formed; an empty set means the
    admin disabled every source.
    """
    if not isinstance(payload, dict):
        return None
    try:
        caps = payload["ocs"]["data"]["capabilities"]
    except (KeyError, TypeError):
        return None
    if not isinstance(caps, dict):
        return None
    block = caps.get("astrolabe")
    if not isinstance(block, dict):
        return None
    semantic = block.get("semantic_search")
    if not isinstance(semantic, dict):
        return None
    raw = semantic.get("enabled_doc_types")
    if not isinstance(raw, list):
        return None
    return frozenset(dt for dt in raw if isinstance(dt, str) and dt)


async def allowed_doc_types(
    client: _CapabilitiesClientProtocol, user_id: str
) -> frozenset[str] | None:
    """Admin-approved doc types for ``user_id``, or ``None`` for "no restriction".

    Cached per user with a short TTL (+ LRU eviction). Failures are not cached so
    a transient OCS hiccup retries on the next call. Fail-open: a missing
    capability block or an error yields ``None`` so search remains available.
    """
    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        _cache.move_to_end(user_id)  # mark recently used (LRU)
        return cached[1]

    try:
        payload = await client.capabilities()
    except Exception as exc:  # noqa: BLE001 — degrade gracefully (fail-open)
        logger.warning(
            "Astrolabe capabilities unavailable for user %s (%s); "
            "not restricting doc types this cycle",
            user_id,
            exc,
        )
        return None  # don't cache failures — retry next call

    result = _parse_enabled_doc_types(payload)
    _cache[user_id] = (now, result)
    # New key: __setitem__ already appends (no-op). Existing expired key: the
    # update keeps its old position, so move it to the end to preserve LRU order.
    _cache.move_to_end(user_id)
    while len(_cache) > _CACHE_MAXSIZE:
        _cache.popitem(last=False)  # evict least-recently-used
    return result


def is_doc_type_allowed(doc_type: str, allowed: frozenset[str] | None) -> bool:
    """Whether ``doc_type`` may be indexed/searched given an allow-set.

    ``allowed=None`` means "no restriction" (fail-open / older Astrolabe), so
    everything is permitted.
    """
    return allowed is None or doc_type in allowed


def clear_cache() -> None:
    """Test hook: drop all cached entries."""
    _cache.clear()
