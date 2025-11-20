"""Placeholder point management for Qdrant state tracking.

Placeholders are zero-vector points stored in Qdrant to track document processing
state. They prevent duplicate work by marking documents as "in-flight" during the
gap between scanner queuing and processor completion.

Architecture:
- Scanner writes placeholders when queuing documents for processing
- Processor deletes placeholders and writes real vectors after processing
- All user-facing queries filter out placeholders (is_placeholder: False)

Placeholders contain:
- Zero vectors (dimension from embedding service)
- is_placeholder: True flag (for filtering)
- status: "pending", "processing", "completed", "failed"
- modified_at, etag from source document
- queued_at timestamp
"""

import logging
import time
import uuid

from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


def _generate_placeholder_id(doc_type: str, doc_id: str | int) -> str:
    """Generate deterministic UUID for placeholder point.

    Args:
        doc_type: Document type (note, file, etc.)
        doc_id: Document ID

    Returns:
        UUID string for point ID
    """
    point_name = f"{doc_type}:{doc_id}:placeholder"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, point_name))


async def write_placeholder_point(
    doc_id: str | int,
    doc_type: str,
    user_id: str,
    modified_at: int,
    etag: str = "",
    file_path: str | None = None,
) -> None:
    """Write a placeholder point to Qdrant to mark document as queued.

    This should be called by the scanner BEFORE queuing a document for processing.
    The placeholder prevents duplicate work if the scanner runs again before
    processing completes.

    Args:
        doc_id: Document ID (int for notes/files)
        doc_type: Document type (note, file, etc.)
        user_id: User ID who owns the document
        modified_at: Document modification timestamp
        etag: Document ETag (if available)
        file_path: File path (for files only)

    Raises:
        Exception: If Qdrant write fails
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()
        embedding_service = get_embedding_service()

        # Get dimension dynamically (never hardcode)
        dimension = embedding_service.get_dimension()

        # Create zero vectors
        zero_dense = [0.0] * dimension

        # Create empty sparse vector for placeholders
        # Use models.SparseVector with empty indices/values
        from qdrant_client import models

        empty_sparse = models.SparseVector(indices=[], values=[])

        # Generate deterministic point ID
        point_id = _generate_placeholder_id(doc_type, doc_id)

        # Build payload
        payload = {
            "user_id": user_id,
            "doc_id": doc_id,
            "doc_type": doc_type,
            "is_placeholder": True,
            "status": "pending",
            "modified_at": modified_at,
            "etag": etag,
            "queued_at": int(time.time()),
        }

        # Add file_path for files
        if doc_type == "file" and file_path:
            payload["file_path"] = file_path

        # Create placeholder point
        point = PointStruct(
            id=point_id,
            vector={
                "dense": zero_dense,
                "sparse": empty_sparse,  # Empty sparse vector for placeholders
            },
            payload=payload,
        )

        # Upsert to Qdrant
        await qdrant_client.upsert(
            collection_name=settings.get_collection_name(),
            points=[point],
            wait=True,
        )

        logger.debug(
            f"Wrote placeholder for {doc_type}_{doc_id} (user={user_id}, "
            f"modified_at={modified_at})"
        )

    except Exception as e:
        logger.error(
            f"Failed to write placeholder for {doc_type}_{doc_id}: {e}",
            exc_info=True,
        )
        raise


async def query_document_metadata(
    doc_id: str | int,
    doc_type: str,
    user_id: str,
) -> dict | None:
    """Query Qdrant for existing document entry (placeholder or real).

    Returns the payload of the first matching point, which could be:
    - A placeholder (is_placeholder: True)
    - A real indexed document (is_placeholder: False or missing)
    - None if document not in Qdrant

    Args:
        doc_id: Document ID
        doc_type: Document type
        user_id: User ID

    Returns:
        Payload dict if found, None otherwise
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        # Query for any entry matching doc_id, doc_type, user_id
        scroll_result = await qdrant_client.scroll(
            collection_name=settings.get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
                ]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )

        if scroll_result[0]:
            point = scroll_result[0][0]
            return dict(point.payload)

        return None

    except Exception as e:
        logger.warning(f"Error querying document metadata for {doc_type}_{doc_id}: {e}")
        return None


async def delete_placeholder_point(
    doc_id: str | int,
    doc_type: str,
    user_id: str,
) -> None:
    """Delete a placeholder point from Qdrant.

    This should be called by the processor BEFORE writing real vectors.
    We delete the placeholder to avoid duplicates, then write the real chunks.

    Args:
        doc_id: Document ID
        doc_type: Document type
        user_id: User ID

    Raises:
        Exception: If Qdrant delete fails
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        # Delete by filter (in case there are multiple chunks from old indexing)
        await qdrant_client.delete(
            collection_name=settings.get_collection_name(),
            points_selector=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
                    FieldCondition(key="is_placeholder", match=MatchValue(value=True)),
                ]
            ),
        )

        logger.debug(f"Deleted placeholder for {doc_type}_{doc_id} (user={user_id})")

    except Exception as e:
        logger.error(
            f"Failed to delete placeholder for {doc_type}_{doc_id}: {e}",
            exc_info=True,
        )
        raise


async def update_placeholder_status(
    doc_id: str | int,
    doc_type: str,
    user_id: str,
    status: str,
) -> None:
    """Update the status field of a placeholder point.

    Status values:
    - "pending": Queued for processing
    - "processing": Currently being processed
    - "completed": Processing completed successfully
    - "failed": Processing failed

    Args:
        doc_id: Document ID
        doc_type: Document type
        user_id: User ID
        status: New status value

    Raises:
        Exception: If Qdrant update fails
    """
    try:
        qdrant_client = await get_qdrant_client()
        settings = get_settings()

        # Update payload using set_payload
        await qdrant_client.set_payload(
            collection_name=settings.get_collection_name(),
            payload={"status": status},
            points=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value=doc_type)),
                    FieldCondition(key="is_placeholder", match=MatchValue(value=True)),
                ]
            ),
        )

        logger.debug(
            f"Updated placeholder status for {doc_type}_{doc_id} to '{status}' "
            f"(user={user_id})"
        )

    except Exception as e:
        logger.warning(
            f"Failed to update placeholder status for {doc_type}_{doc_id}: {e}"
        )
        # Don't raise - status updates are non-critical


def get_placeholder_filter() -> FieldCondition:
    """Get a filter condition to exclude placeholders from queries.

    Add this to all user-facing search/visualization queries to ensure
    placeholders are never returned to users.

    Returns:
        FieldCondition that filters out is_placeholder: True

    Example:
        Filter(
            must=[
                get_placeholder_filter(),  # Exclude placeholders
                FieldCondition(key="user_id", match=MatchValue(value=user_id)),
            ]
        )
    """
    return FieldCondition(
        key="is_placeholder",
        match=MatchValue(value=False),
    )
