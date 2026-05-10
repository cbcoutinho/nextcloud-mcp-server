"""Lazy eviction of stale documents from the vector index.

Used by the verify-on-read path (ADR-019) to remove points for documents that
have been deleted or unshared in Nextcloud but not yet reconciled by the
webhook/scanner sync loop. Eviction is fire-and-forget from the search hot
path; failures are logged but never propagated, since the next query will
simply re-verify and re-attempt.
"""

import logging

from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


async def delete_document_points(
    doc_id: str,
    doc_type: str,
    user_id: str,
) -> None:
    """Remove all Qdrant points for a single document.

    Deletes both real chunk points and any leftover placeholder points for the
    given (user_id, doc_id, doc_type) tuple. Safe to call when the document is
    not present — Qdrant returns successfully with zero points affected.

    Args:
        doc_id: Document ID (str — keyword-indexed in Qdrant payload)
        doc_type: Document type (note, file, deck_card, news_item)
        user_id: Owner of the points being evicted

    Raises:
        Exception: If the underlying Qdrant client raises. Callers in the
            search hot path should catch and log; eviction failures must not
            block search responses.
    """
    qdrant_client = await get_qdrant_client()
    settings = get_settings()

    await qdrant_client.delete(
        collection_name=settings.get_collection_name(),
        points_selector=Filter(
            must=[
                FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
            ]
        ),
    )

    logger.info(
        "Evicted Qdrant points for %s_%s (user=%s); "
        "document was inaccessible at verification time",
        doc_type,
        doc_id,
        user_id,
    )
