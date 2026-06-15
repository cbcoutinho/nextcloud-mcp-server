"""Global purge of indexed vectors by doc type (admin consent enforcement).

When an admin disables a content source for semantic search in Astrolabe,
consent is binding on data-at-rest: the already-indexed content for that
source's doc type(s) must be deleted, not merely hidden. Astrolabe calls the
``/api/v1/vector-sync/purge`` route on disable, which delegates here.

The purge is global (every owner) because the admin disable is a global
decision. It is safe to call for a doc type with no indexed points — Qdrant
deletes zero points and reports a count of 0.
"""

from __future__ import annotations

import logging

from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


def _doc_type_filter(doc_type: str) -> Filter:
    return Filter(
        must=[FieldCondition(key="doc_type", match=MatchValue(value=doc_type))]
    )


async def purge_doc_types(doc_types: list[str]) -> dict[str, int]:
    """Delete every indexed point whose ``doc_type`` is in ``doc_types``.

    Returns a mapping of doc_type -> number of points deleted (counted before
    deletion). Each doc type is purged independently so a failure on one does
    not abort the rest; failures re-raise after the loop only if every doc type
    failed, otherwise partial progress is returned.

    The count is taken just before the delete (two separate Qdrant calls), so
    it is approximate — a point indexed in the gap is deleted but not counted.
    This is acceptable: indexing of a disabled source is already gated upstream,
    so the window is effectively empty in practice.
    """
    qdrant_client = await get_qdrant_client()
    collection = get_settings().get_collection_name()

    purged: dict[str, int] = {}
    last_error: Exception | None = None
    for doc_type in dict.fromkeys(doc_types):  # de-dupe, preserve order
        flt = _doc_type_filter(doc_type)
        try:
            count_result = await qdrant_client.count(
                collection_name=collection,
                count_filter=flt,
                exact=True,
            )
            await qdrant_client.delete(
                collection_name=collection,
                points_selector=flt,
            )
            purged[doc_type] = int(count_result.count)
            logger.info(
                "Purged %d indexed point(s) for disabled doc_type=%s",
                purged[doc_type],
                doc_type,
            )
        except Exception as exc:  # noqa: BLE001 — record and continue
            last_error = exc
            logger.exception(
                "Failed to purge indexed points for doc_type=%s",
                doc_type,
            )

    if not purged and last_error is not None:
        # Nothing succeeded — surface the failure to the caller (HTTP 500).
        raise last_error
    return purged
