"""Shared helpers for asserting vector-sync visibility in integration tests.

Kept dependency-light (no Playwright) so both the multi-user-basic UI tests and
the single-user sampling tests can import it.
"""

import json
import logging

logger = logging.getLogger(__name__)


async def document_is_searchable(
    mcp_client, search_term: str, note_id: int | None = None
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
            arguments={"query": search_term, "limit": 10, "score_threshold": 0.0},
        )
    except Exception as e:  # transient transport/availability blip — keep polling
        logger.debug("Semantic search poll failed: %s", e)
        return False
    if search.isError:
        logger.debug("Semantic search poll error: %s", search)
        return False

    results = json.loads(search.content[0].text).get("results", [])
    needle = search_term.lower()
    for r in results:
        if note_id is not None:
            if r.get("id") == note_id and r.get("doc_type") == "note":
                return True
        elif needle in f"{r.get('title', '')} {r.get('excerpt', '')}".lower():
            return True
    return False
