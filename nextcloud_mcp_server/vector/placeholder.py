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

from qdrant_client import AsyncQdrantClient, models
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)

# Stamped on every placeholder this Pod-process writes. A fresh UUID per
# process means a restarted Pod sees its predecessor's placeholders as
# "not mine" and deletes them in ``sweep_orphan_placeholders`` at startup,
# instead of waiting out the ``5 × VECTOR_SYNC_SCAN_INTERVAL`` staleness
# gate (~5h with the deployed 1h scan interval). Card #101.
_INSTANCE_ID = str(uuid.uuid4())


def _generate_placeholder_id(doc_type: str, doc_id: str) -> str:
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
    doc_id: str,
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
        doc_id: Document ID (always str — see DocumentTask)
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

        # Size the dense zero-vector to match the collection's dense slot. In
        # keyword mode (SEARCH_MODE=keyword, dense_enabled=False) the collection's
        # dense slot is created sized to SIMPLE_EMBEDDING_DIMENSION — NOT the
        # embedding provider's dimension (see qdrant_client collection creation) —
        # and the deployment may have no text-embedding endpoint at all. So gate on
        # dense_enabled and never touch the embedding service in keyword mode;
        # otherwise a Mistral-sized (e.g. 1024) placeholder vector is rejected by
        # the 384-dim slot and the pre-enqueue placeholder write aborts the whole
        # scan (no document ever gets indexed).
        if settings.dense_enabled:
            dimension = get_embedding_service().get_dimension()
        else:
            dimension = settings.simple_embedding_dimension

        # Create zero vectors
        zero_dense = [0.0] * dimension

        # Create empty sparse vector for placeholders
        # Use models.SparseVector with empty indices/values
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
            # Pod-process identity. ``sweep_orphan_placeholders`` uses
            # the (placeholder.instance_id != _INSTANCE_ID) predicate to
            # detect orphans from a crashed predecessor Pod.
            "instance_id": _INSTANCE_ID,
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
            "Wrote placeholder for %s_%s (user=%s, modified_at=%s)",
            doc_type,
            doc_id,
            user_id,
            modified_at,
        )

    except Exception as e:
        logger.error(
            "Failed to write placeholder for %s_%s: %s",
            doc_type,
            doc_id,
            e,
        )
        raise


async def query_document_metadata(
    doc_id: str,
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
        logger.warning(
            "Error querying document metadata for %s_%s: %s", doc_type, doc_id, e
        )
        return None


async def delete_placeholder_point(
    doc_id: str,
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

        logger.debug(
            "Deleted placeholder for %s_%s (user=%s)", doc_type, doc_id, user_id
        )

    except Exception as e:
        logger.error(
            "Failed to delete placeholder for %s_%s: %s",
            doc_type,
            doc_id,
            e,
        )
        raise


async def update_placeholder_status(
    doc_id: str,
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
            "Updated placeholder status for %s_%s to '%s' (user=%s)",
            doc_type,
            doc_id,
            status,
            user_id,
        )

    except Exception as e:
        logger.warning(
            "Failed to update placeholder status for %s_%s: %s", doc_type, doc_id, e
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


# Batch size for the orphan-sweep scroll + delete loop. Big enough to
# keep round-trip count down, small enough that a single delete payload
# isn't unreasonable. Matches the order of magnitude of a per-tenant
# placeholder count (108 in the originating incident).
_ORPHAN_SWEEP_BATCH_SIZE = 100


async def sweep_orphan_placeholders(
    qdrant_client: AsyncQdrantClient,
    collection: str,
    *,
    batch_size: int = _ORPHAN_SWEEP_BATCH_SIZE,
) -> tuple[int, int]:
    """Delete placeholder points written by a previous Pod-process.

    Scrolls all ``is_placeholder=true`` points in the collection,
    paginated. For each batch, partitions points by whether their
    ``instance_id`` payload field matches the current Pod's
    ``_INSTANCE_ID``. Orphans (different ``instance_id`` OR field
    absent — back-compat for placeholders written by pre-fix Pod
    versions) are deleted by point ID; own-Pod placeholders are
    left alone for the scanner's staleness gate to handle normally.

    Called once at Pod startup from ``app.starlette_lifespan`` —
    NOT periodically. The own-Pod path relies on the existing
    ``5 × VECTOR_SYNC_SCAN_INTERVAL`` gate; this helper only
    addresses the cross-Pod-restart gap. Card #101.

    Args:
        qdrant_client: Async Qdrant client.
        collection: Target collection (resolved name from
            ``settings.get_collection_name()``, not the raw config key).
        batch_size: Scroll page size. Default 100 — small enough that
            a single delete payload is reasonable, large enough that
            round-trip count stays bounded for typical placeholder
            counts (~hundreds per tenant).

    Returns:
        ``(swept, kept)`` — number of placeholders deleted as orphans,
        and number left in place as belonging to the current Pod.
    """
    placeholder_filter = Filter(
        must=[
            FieldCondition(key="is_placeholder", match=MatchValue(value=True)),
        ]
    )
    swept = 0
    kept = 0
    offset = None

    while True:
        points, offset = await qdrant_client.scroll(
            collection_name=collection,
            scroll_filter=placeholder_filter,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break

        orphan_ids = []
        for point in points:
            payload = point.payload or {}
            # Dead-letter markers (vector/dead_letter.py) reuse is_placeholder=True
            # for the search exclusion but are DURABLE terminal-state records, not
            # in-flight placeholders -- they carry no/foreign instance_id and must
            # survive a Pod restart, so never sweep them as orphans.
            if payload.get("dead_letter") is True:
                # Tenant-wide and always kept (not Pod-scoped); counted under
                # ``kept`` only because the sweep's tally has no separate bucket.
                kept += 1
                continue
            point_instance = payload.get("instance_id")
            if point_instance == _INSTANCE_ID:
                kept += 1
            else:
                orphan_ids.append(point.id)

        if orphan_ids:
            await qdrant_client.delete(
                collection_name=collection,
                points_selector=orphan_ids,
            )
            swept += len(orphan_ids)

        # ``offset is None`` signals the scroll cursor has been
        # exhausted — Qdrant's contract for paginated scroll.
        if offset is None:
            break

    return swept, kept
