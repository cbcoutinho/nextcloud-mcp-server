"""Shared helpers for asserting vector-sync visibility in integration tests.

Kept dependency-light (no Playwright) so both the multi-user-basic UI tests and
the single-user sampling tests can import it.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def document_is_searchable(
    mcp_client: Any, search_term: str, note_id: int | None = None
) -> bool:
    """Return True once a freshly-created document is retrievable.

    Polls ``nc_semantic_search`` (hybrid: an exact unique term reliably matches
    on the keyword side) and matches by ``note_id`` when provided, otherwise by
    the term appearing in a result's title/excerpt. Transient errors return
    False so callers can keep polling.
    """
    try:
        search = await mcp_client.call_tool(
            "nc_semantic_search",
            # limit is generous: a fresh note can sit below seed data (e.g. deck
            # cards) in a crowded corpus, and the query is cheap.
            arguments={"query": search_term, "limit": 50, "score_threshold": 0.0},
        )
    except Exception as e:  # transient transport/availability blip — keep polling
        logger.debug("Semantic search poll failed: %s", e)
        return False
    if search.isError:
        logger.debug("Semantic search poll error: %s", search)
        return False

    try:
        results = json.loads(search.content[0].text).get("results", [])
    except (IndexError, ValueError) as e:  # empty content / malformed JSON
        logger.debug("Semantic search parse failed: %s", e)
        return False

    # Token match (not contiguous substring) so multi-word terms work in the
    # note_id-less fallback path.
    tokens = search_term.lower().split()
    for r in results:
        if note_id is not None:
            # str-coerce both sides: nc_semantic_search returns int ids today,
            # but the Astrolabe API serialises some ids as strings — match the
            # defensive comparison in _poll_astrolabe_search_for_note so a future
            # schema change can't silently break the match.
            if str(r.get("id")) == str(note_id):
                if r.get("doc_type") == "note":
                    return True
                # id matched but not a note — surface possible schema drift at
                # WARNING (CI runs --log-cli-level=WARN) instead of letting the
                # caller time out with a generic message.
                logger.warning(
                    "search hit id=%s has doc_type=%s (expected note)",
                    note_id,
                    r.get("doc_type"),
                )
        else:
            haystack = f"{r.get('title', '')} {r.get('excerpt', '')}".lower()
            if tokens and all(t in haystack for t in tokens):
                return True
    return False
