"""Scanner task for vector database synchronization.

Periodically scans enabled users' content and queues changed documents for processing.
"""

import asyncio
import logging
from dataclasses import dataclass

import anyio
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


async def scanner_task(
    document_queue: asyncio.Queue,
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    nc_client: NextcloudClient,
    user_id: str,
):
    """
    Periodic scanner that detects changed documents for enabled user.

    For BasicAuth mode, scans a single user with credentials available at runtime.

    Args:
        document_queue: Queue to enqueue changed documents
        shutdown_event: Event signaling shutdown
        wake_event: Event to trigger immediate scan
        nc_client: Authenticated Nextcloud client
        user_id: User to scan
    """
    logger.info(f"Scanner task started for user: {user_id}")
    settings = get_settings()

    while not shutdown_event.is_set():
        try:
            # Scan user documents
            await scan_user_documents(
                user_id=user_id,
                document_queue=document_queue,
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

    logger.info("Scanner task stopped")


async def scan_user_documents(
    user_id: str,
    document_queue: asyncio.Queue,
    nc_client: NextcloudClient,
    initial_sync: bool = False,
):
    """
    Scan a single user's documents and queue changes.

    Args:
        user_id: User to scan
        document_queue: Queue to enqueue changed documents
        nc_client: Authenticated Nextcloud client
        initial_sync: If True, queue all documents (first-time sync)
    """
    logger.info(f"Scanning documents for user: {user_id}")

    # Fetch all notes from Nextcloud
    notes = await nc_client.notes.list_notes()
    logger.debug(f"Found {len(notes)} notes for {user_id}")

    if initial_sync:
        # Queue everything on first sync
        for note in notes:
            await document_queue.put(
                DocumentTask(
                    user_id=user_id,
                    doc_id=str(note["id"]),
                    doc_type="note",
                    operation="index",
                    modified_at=note["modified"],
                )
            )
        logger.info(f"Queued {len(notes)} documents for initial sync: {user_id}")
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
    for note in notes:
        doc_id = str(note["id"])
        indexed_at = indexed_docs.get(doc_id)

        # Queue if never indexed or modified since last index
        if indexed_at is None or note["modified"] > indexed_at:
            await document_queue.put(
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
    nextcloud_doc_ids = {str(note["id"]) for note in notes}
    for doc_id in indexed_docs:
        if doc_id not in nextcloud_doc_ids:
            await document_queue.put(
                DocumentTask(
                    user_id=user_id,
                    doc_id=doc_id,
                    doc_type="note",
                    operation="delete",
                    modified_at=0,
                )
            )
            queued += 1

    if queued > 0:
        logger.info(f"Queued {queued} documents for incremental sync: {user_id}")
    else:
        logger.debug(f"No changes detected for {user_id}")
