"""Usage metering for semantic search.

Lives here rather than in ``server/semantic.py`` because semantic search has TWO
entrypoints — the ``nc_semantic_search`` MCP tool and ``POST /api/v1/search`` —
and ``api/`` must not import from ``server/``. ``server.semantic`` re-exports
``record_search_usage`` so existing callers and tests keep working.
"""

import logging

from nextcloud_mcp_server.usage.store import UsageEventStore

logger = logging.getLogger(__name__)

# Cap how many doc_types we copy into a usage-metering metadata row. doc_types
# is caller-supplied and (unlike path_prefixes) has no max_length on the tool
# signature, so an adversarial caller could pass a huge list. The CP rollup
# ignores metadata for billing (GROUP BY day, metric) and the value is bound
# parameterized, so this is not a billing/injection risk — the cap just keeps
# a single JSONB row from ballooning. 16 is generous headroom over the handful
# of real indexed doc types.
_USAGE_METADATA_MAX_DOC_TYPES = 16


async def record_search_usage(
    *,
    enabled: bool,
    user_id: str,
    fusion: str,
    doc_types: list[str] | None,
    token_count: int | None,
    surface: str = "mcp",
) -> None:
    """Record the billable ``tokens_embedded`` event for one semantic search.

    The value is the query embedding's token count (provider-reported or
    estimated) — the unit upstream providers bill on, and the same metric the
    indexing path records for chunk embeddings (Deck #67).

    Called once per search from each entrypoint: ``nc_semantic_search``
    (``surface="mcp"``) and ``POST /api/v1/search`` (``surface="http"``). Do not
    add a second hook *within* either path — the guard is against double-counting
    one search, not against a second entrypoint.

    Best-effort and flag-gated: a metering failure is logged and never breaks
    the search. Unlike the indexing path's chunk-count guard, a 0-token query is
    still recorded (the query embedding ran); a zero-value row is a no-op at the
    Stripe ``sum`` aggregation.

    Privacy note: ``user_id`` stays tenant-local — the CP rollup aggregates
    GROUP BY (day, metric) into ``usage_daily`` (no metadata column), so nothing
    here propagates to Stripe; it is retained only to keep Deck #67's future
    per-user attribution derivable from app-DB metadata without a re-migration.

    Args:
        enabled: ``USAGE_METERING_ENABLED``; when false this is a no-op.
        user_id: The authenticated caller (tenant-local, see privacy note).
        fusion: Fusion mode label (``rrf`` | ``dbsf``).
        doc_types: Requested doc_type filter, or ``None`` for all types.
        token_count: Query-embedding tokens; ``None`` is recorded as 0.
        surface: Which entrypoint recorded this — ``"mcp"`` or ``"http"``. Kept
            in metadata so the two can be separated in the rollup; the HTTP
            surface began recording later than the MCP one, so a step change in
            ``tokens_embedded`` is expected at that point rather than a
            regression.
    """
    if not enabled:
        return
    try:
        store = await UsageEventStore.shared()
        await store.record_usage_event(
            metric="tokens_embedded",
            value=token_count or 0,
            metadata={
                "user_id": user_id,
                "fusion": fusion,
                "surface": surface,
                # Bounded copy — see _USAGE_METADATA_MAX_DOC_TYPES. Both None and
                # [] normalize to null so a future metadata->'doc_types' IS NULL
                # query counts the all-types case consistently.
                "doc_types": (
                    doc_types[:_USAGE_METADATA_MAX_DOC_TYPES] if doc_types else None
                ),
            },
            # The caller already confirmed the flag, so pass enabled=True
            # directly — the store then skips a second uncached Settings build on
            # this hot query path (ADR-024).
            enabled=True,
        )
    except Exception:
        # Reached only when shared()/store construction itself raises
        # (record_usage_event swallows its own write failures). Metering is on,
        # so warn — a silent DEBUG line would hide "operator enabled metering
        # but gets no data".
        logger.warning("usage metering hook (tokens_embedded) skipped")
