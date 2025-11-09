"""Scanner task for vector database synchronization.

Periodically scans enabled users' content and queues changed documents for processing.
"""

import logging
import time
from dataclasses import dataclass

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


@dataclass
class DocumentTask:
    """Document task for processing queue."""

    user_id: str
    doc_id: str
    doc_type: str  # "note", "file", "calendar"
    operation: str  # "index" or "delete"
    modified_at: int


# Track documents potentially deleted (grace period before actual deletion)
# Format: {(user_id, doc_id): first_missing_timestamp}
_potentially_deleted: dict[tuple[str, str], float] = {}


async def scanner_task(
    send_stream: MemoryObjectSendStream[DocumentTask],
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    nc_client: NextcloudClient,
    user_id: str,
):
    """
    Periodic scanner that detects changed documents for enabled user.

    For BasicAuth mode, scans a single user with credentials available at runtime.

    Args:
        send_stream: Stream to send changed documents to processors
        shutdown_event: Event signaling shutdown
        wake_event: Event to trigger immediate scan
        nc_client: Authenticated Nextcloud client
        user_id: User to scan
    """
    logger.info(f"Scanner task started for user: {user_id}")
    settings = get_settings()

    async with send_stream:
        while not shutdown_event.is_set():
            try:
                # Scan user documents
                await scan_user_documents(
                    user_id=user_id,
                    send_stream=send_stream,
                    nc_client=nc_client,
                )

            except Exception as e:
                logger.error(f"Scanner error: {e}", exc_info=True)

            # Sleep until next interval or wake event
            try:
                with anyio.move_on_after(settings.vector_sync_scan_interval):
                    # Wait for wake event or shutdown (whichever comes first)
                    await wake_event.wait()
            except anyio.get_cancelled_exc_class():
                # Shutdown, exit loop
                break

    logger.info("Scanner task stopped - stream closed")


async def scan_user_documents(
    user_id: str,
    send_stream: MemoryObjectSendStream[DocumentTask],
    nc_client: NextcloudClient,
    initial_sync: bool = False,
):
    """
    Scan a single user's documents and send changes to processor stream.

    Args:
        user_id: User to scan
        send_stream: Stream to send changed documents to processors
        nc_client: Authenticated Nextcloud client
        initial_sync: If True, send all documents (first-time sync)
    """
    logger.info(f"Scanning documents for user: {user_id}")

    # Fetch all notes from Nextcloud
    notes = [note async for note in nc_client.notes.get_all_notes()]
    logger.debug(f"Found {len(notes)} notes for {user_id}")

    if initial_sync:
        # Send everything on first sync
        for note in notes:
            await send_stream.send(
                DocumentTask(
                    user_id=user_id,
                    doc_id=str(note["id"]),
                    doc_type="note",
                    operation="index",
                    modified_at=note["modified"],
                )
            )
        logger.info(f"Sent {len(notes)} documents for initial sync: {user_id}")
        return

    # Get indexed state from Qdrant
    qdrant_client = await get_qdrant_client()
    scroll_result = await qdrant_client.scroll(
        collection_name=get_settings().qdrant_collection,
        scroll_filter=Filter(
            must=[
                FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                FieldCondition(key="doc_type", match=MatchValue(value="note")),
            ]
        ),
        with_payload=["doc_id", "indexed_at"],
        with_vectors=False,
        limit=10000,
    )

    indexed_docs = {
        point.payload["doc_id"]: point.payload["indexed_at"]
        for point in scroll_result[0]
    }

    logger.debug(f"Found {len(indexed_docs)} indexed documents in Qdrant")

    # Compare and queue changes
    queued = 0
    nextcloud_doc_ids = {str(note["id"]) for note in notes}

    for note in notes:
        doc_id = str(note["id"])
        indexed_at = indexed_docs.get(doc_id)

        # If document reappeared, remove from potentially_deleted
        doc_key = (user_id, doc_id)
        if doc_key in _potentially_deleted:
            logger.debug(
                f"Document {doc_id} reappeared, removing from deletion grace period"
            )
            del _potentially_deleted[doc_key]

        # Send if never indexed or modified since last index
        if indexed_at is None or note["modified"] > indexed_at:
            await send_stream.send(
                DocumentTask(
                    user_id=user_id,
                    doc_id=doc_id,
                    doc_type="note",
                    operation="index",
                    modified_at=note["modified"],
                )
            )
            queued += 1

    # Check for deleted documents (in Qdrant but not in Nextcloud)
    # Use grace period: only delete after 2 consecutive scans confirm absence
    settings = get_settings()
    grace_period = settings.vector_sync_scan_interval * 1.5  # Allow 1.5 scan intervals
    current_time = time.time()

    for doc_id in indexed_docs:
        if doc_id not in nextcloud_doc_ids:
            doc_key = (user_id, doc_id)

            if doc_key in _potentially_deleted:
                # Already marked as potentially deleted, check if grace period elapsed
                first_missing_time = _potentially_deleted[doc_key]
                time_missing = current_time - first_missing_time

                if time_missing >= grace_period:
                    # Grace period elapsed, send for deletion
                    logger.info(
                        f"Document {doc_id} missing for {time_missing:.1f}s "
                        f"(>{grace_period:.1f}s grace period), sending deletion"
                    )
                    await send_stream.send(
                        DocumentTask(
                            user_id=user_id,
                            doc_id=doc_id,
                            doc_type="note",
                            operation="delete",
                            modified_at=0,
                        )
                    )
                    queued += 1
                    # Remove from tracking after sending deletion
                    del _potentially_deleted[doc_key]
                else:
                    logger.debug(
                        f"Document {doc_id} still missing "
                        f"({time_missing:.1f}s/{grace_period:.1f}s grace period)"
                    )
            else:
                # First time missing, add to grace period tracking
                logger.debug(
                    f"Document {doc_id} missing for first time, starting grace period"
                )
                _potentially_deleted[doc_key] = current_time

    if queued > 0:
        logger.info(f"Sent {queued} documents for incremental sync: {user_id}")
    else:
        logger.debug(f"No changes detected for {user_id}")
