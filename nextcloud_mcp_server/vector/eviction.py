"""Lazy eviction of stale documents from the vector index.

Used by the verify-on-read path (ADR-019) to remove points for documents that
have been deleted or unshared in Nextcloud but not yet reconciled by the
webhook/scanner sync loop. Eviction is fire-and-forget from the search hot
path; failures are logged but never propagated, since the next query will
simply re-verify and re-attempt.
"""

import logging

from nextcloud_mcp_server.vector.sharing_state import release_document_for_user

logger = logging.getLogger(__name__)


async def delete_document_points(
    doc_id: str,
    doc_type: str,
    user_id: str,
) -> None:
    """Revoke one user's access to a document's points (verify-on-read eviction).

    A document can be indexed once and shared across users (user-agnostic point
    IDs), so eviction must *release* this user rather than blindly delete: it
    drops ``user:<user_id>`` from the point's ``acl_principals`` and removes the
    points only when no reader remains. Legacy points without a principal set
    fall back to the original per-user delete. Safe to call when the document is
    not present — Qdrant returns successfully with zero points affected.

    Args:
        doc_id: Document ID (str — keyword-indexed in Qdrant payload)
        doc_type: Document type (note, file, deck_card, news_item)
        user_id: User whose access is being revoked

    Raises:
        Exception: If the underlying Qdrant client raises. Callers in the
            search hot path should catch and log; eviction failures must not
            block search responses.
    """
    await release_document_for_user(doc_id, doc_type, user_id)

    logger.info(
        "Released %s_%s for user=%s; document was inaccessible at verification time",
        doc_type,
        doc_id,
        user_id,
    )
