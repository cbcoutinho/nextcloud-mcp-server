"""Scanner task for vector database synchronization.

Periodically scans enabled users' content and queues changed documents for processing.
"""

import logging
import os
import time
from dataclasses import dataclass

import anyio
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectSendStream
from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.observability.metrics import record_vector_sync_scan
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.vector.placeholder import (
    query_document_metadata,
    write_placeholder_point,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


@dataclass
class DocumentTask:
    """Document task for processing queue."""

    user_id: str
    doc_id: int | str  # int for files/notes, str for legacy
    doc_type: str  # "note", "file", "calendar"
    operation: str  # "index" or "delete"
    modified_at: int
    file_path: str | None = None  # File path for files (when doc_id is file_id)


# Track documents potentially deleted (grace period before actual deletion)
# Format: {(user_id, doc_id): first_missing_timestamp}
_potentially_deleted: dict[tuple[str, str], float] = {}


async def get_last_indexed_timestamp(user_id: str) -> int | None:
    """Get the most recent indexed_at timestamp for user's notes in Qdrant.

    This timestamp can be used as pruneBefore parameter to optimize data transfer
    when fetching notes - only notes modified after this timestamp will be sent
    with full data.

    Args:
        user_id: User to query

    Returns:
        Unix timestamp of most recently indexed note, or None if no notes indexed yet
    """
    try:
        qdrant_client = await get_qdrant_client()

        # Query for user's notes, ordered by indexed_at descending, limit 1
        scroll_result = await qdrant_client.scroll(
            collection_name=get_settings().get_collection_name(),
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                    FieldCondition(key="doc_type", match=MatchValue(value="note")),
                ]
            ),
            with_payload=["indexed_at"],
            with_vectors=False,
            limit=10000,  # Get all to find max
        )

        # Find max indexed_at across all results
        num_points = len(scroll_result[0]) if scroll_result[0] else 0
        logger.info(f"Found {num_points} indexed notes in Qdrant for user {user_id}")

        if scroll_result[0]:
            timestamps = [
                point.payload.get("indexed_at", 0) for point in scroll_result[0]
            ]
            max_timestamp = max(timestamps)
            logger.info(
                f"Max indexed_at: {max_timestamp}, timestamps sample: {timestamps[:3]}"
            )
            return int(max_timestamp) if max_timestamp > 0 else None

        logger.info(f"No indexed notes found for user {user_id}")
        return None
    except Exception as e:
        logger.warning(f"Failed to get last indexed timestamp: {e}", exc_info=True)
        return None


async def scanner_task(
    send_stream: MemoryObjectSendStream[DocumentTask],
    shutdown_event: anyio.Event,
    wake_event: anyio.Event,
    nc_client: NextcloudClient,
    user_id: str,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
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
        task_status: Status object for signaling task readiness
    """
    logger.info(f"Scanner task started for user: {user_id}")
    settings = get_settings()

    # Signal that the task has started and is ready
    task_status.started()

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
    import random

    scan_id = random.randint(1000, 9999)
    logger.info(
        f"[SCAN-{scan_id}] Starting scan for user: {user_id}, initial_sync={initial_sync}"
    )

    with trace_operation(
        "vector_sync.scan_user_documents",
        attributes={
            "vector_sync.operation": "scan",
            "vector_sync.user_id": user_id,
            "vector_sync.initial_sync": initial_sync,
            "vector_sync.scan_id": scan_id,
        },
    ):
        # Calculate prune timestamp for optimized data transfer
        # Only notes modified after this will be sent with full data
        prune_before = (
            None if initial_sync else await get_last_indexed_timestamp(user_id)
        )
        if prune_before:
            logger.info(
                f"[SCAN-{scan_id}] Using pruneBefore={prune_before} to optimize data transfer"
            )

        # For deletion tracking, get all doc_ids in Qdrant (for incremental sync)
        # Note: We no longer bulk-query indexed_at, instead check per-document
        indexed_doc_ids = set()
        if not initial_sync:
            qdrant_client = await get_qdrant_client()
            scroll_result = await qdrant_client.scroll(
                collection_name=get_settings().get_collection_name(),
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                        FieldCondition(key="doc_type", match=MatchValue(value="note")),
                    ]
                ),
                with_payload=["doc_id"],
                with_vectors=False,
                limit=10000,
            )

            indexed_doc_ids = {point.payload["doc_id"] for point in scroll_result[0]}

            logger.debug(f"Found {len(indexed_doc_ids)} indexed documents in Qdrant")

        # Stream notes from Nextcloud and process immediately
        note_count = 0
        queued = 0
        nextcloud_doc_ids = set()

        async for note in nc_client.notes.get_all_notes(prune_before=prune_before):
            note_count += 1
            doc_id = str(note["id"])
            nextcloud_doc_ids.add(doc_id)
            modified_at = note.get("modified", 0)

            if initial_sync:
                # Send everything on first sync - write placeholder first
                await write_placeholder_point(
                    doc_id=doc_id,
                    doc_type="note",
                    user_id=user_id,
                    modified_at=modified_at,
                    etag=note.get("etag", ""),
                )
                await send_stream.send(
                    DocumentTask(
                        user_id=user_id,
                        doc_id=doc_id,
                        doc_type="note",
                        operation="index",
                        modified_at=modified_at,
                    )
                )
                queued += 1
            else:
                # Incremental sync: check if document exists and compare modified_at
                # If document reappeared, remove from potentially_deleted
                doc_key = (user_id, doc_id)
                if doc_key in _potentially_deleted:
                    logger.debug(
                        f"Document {doc_id} reappeared, removing from deletion grace period"
                    )
                    del _potentially_deleted[doc_key]

                # Query Qdrant for existing entry (placeholder or real)
                existing_metadata = await query_document_metadata(
                    doc_id=doc_id, doc_type="note", user_id=user_id
                )

                # Send if never indexed or modified since last index
                # Compare against stored modified_at (not indexed_at!)
                needs_indexing = False
                if existing_metadata is None:
                    # Never seen before
                    needs_indexing = True
                elif existing_metadata.get("modified_at", 0) < modified_at:
                    # Document modified since last indexing
                    needs_indexing = True
                elif existing_metadata.get("is_placeholder", False):
                    # Placeholder exists - check if it's stale (processing may have failed)
                    # Only requeue if placeholder is older than 2x scan interval
                    queued_at = existing_metadata.get("queued_at", 0)
                    placeholder_age = time.time() - queued_at
                    stale_threshold = get_settings().vector_sync_scan_interval * 2
                    if placeholder_age > stale_threshold:
                        logger.debug(
                            f"Found stale placeholder for note {doc_id} "
                            f"(age={placeholder_age:.1f}s), requeuing"
                        )
                        needs_indexing = True
                    else:
                        logger.debug(
                            f"Skipping note {doc_id} with recent placeholder "
                            f"(age={placeholder_age:.1f}s < {stale_threshold:.1f}s)"
                        )

                if needs_indexing:
                    # Write placeholder before queuing
                    await write_placeholder_point(
                        doc_id=doc_id,
                        doc_type="note",
                        user_id=user_id,
                        modified_at=modified_at,
                        etag=note.get("etag", ""),
                    )
                    await send_stream.send(
                        DocumentTask(
                            user_id=user_id,
                            doc_id=doc_id,
                            doc_type="note",
                            operation="index",
                            modified_at=modified_at,
                        )
                    )
                    queued += 1

        # Log and record metrics after streaming
        logger.info(f"[SCAN-{scan_id}] Found {note_count} notes for {user_id}")
        record_vector_sync_scan(note_count)

        if initial_sync:
            logger.info(f"Sent {queued} documents for initial sync: {user_id}")
            return

        # Check for deleted documents (in Qdrant but not in Nextcloud)
        # Use grace period: only delete after 2 consecutive scans confirm absence
        settings = get_settings()
        grace_period = (
            settings.vector_sync_scan_interval * 1.5
        )  # Allow 1.5 scan intervals
        current_time = time.time()

        for doc_id in indexed_doc_ids:
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

        # Scan tagged PDF files (after notes)
        # Get indexed file IDs from Qdrant (for deletion tracking)
        indexed_file_ids = set()
        if not initial_sync:
            file_scroll_result = await qdrant_client.scroll(
                collection_name=settings.get_collection_name(),
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                        FieldCondition(key="doc_type", match=MatchValue(value="file")),
                    ]
                ),
                limit=10000,  # Reasonable limit for file count
                with_payload=["doc_id"],
                with_vectors=False,
            )

            indexed_file_ids = {
                point.payload["doc_id"] for point in file_scroll_result[0]
            }

            logger.debug(f"Found {len(indexed_file_ids)} indexed files in Qdrant")

        # Scan for tagged PDF files
        file_count = 0
        file_queued = 0
        nextcloud_file_ids = set()

        try:
            # Find files with vector-index tag using OCS Tags API
            settings = get_settings()
            tag_name = os.getenv("VECTOR_SYNC_PDF_TAG", "vector-index")
            # Use NextcloudClient.find_files_by_tag() which uses proper OCS API
            # and filters by PDF MIME type
            tagged_files = await nc_client.find_files_by_tag(
                tag_name, mime_type_filter="application/pdf"
            )

            for file_info in tagged_files:
                # Files are already filtered by MIME type in find_files_by_tag()
                file_count += 1
                file_id = file_info["id"]  # Use numeric file ID, not path
                file_path = file_info["path"]  # Keep path for logging
                nextcloud_file_ids.add(file_id)

                # Use last_modified timestamp if available, otherwise use current time
                modified_at = file_info.get("last_modified_timestamp", int(time.time()))
                if isinstance(file_info.get("last_modified"), str):
                    # Parse RFC 2822 date format if needed
                    from email.utils import parsedate_to_datetime

                    try:
                        dt = parsedate_to_datetime(file_info["last_modified"])
                        modified_at = int(dt.timestamp())
                    except (ValueError, KeyError):
                        pass

                if initial_sync:
                    # Send everything on first sync - write placeholder first
                    await write_placeholder_point(
                        doc_id=file_id,
                        doc_type="file",
                        user_id=user_id,
                        modified_at=modified_at,
                        file_path=file_path,
                    )
                    await send_stream.send(
                        DocumentTask(
                            user_id=user_id,
                            doc_id=file_id,  # Use numeric file ID
                            doc_type="file",
                            operation="index",
                            modified_at=modified_at,
                            file_path=file_path,  # Pass file path for content retrieval
                        )
                    )
                    file_queued += 1
                else:
                    # Incremental sync: check if file exists and compare modified_at
                    # If file reappeared, remove from potentially_deleted
                    file_key = (user_id, file_id)
                    if file_key in _potentially_deleted:
                        logger.debug(
                            f"File {file_path} (ID: {file_id}) reappeared, removing from deletion grace period"
                        )
                        del _potentially_deleted[file_key]

                    # Query Qdrant for existing entry (placeholder or real)
                    existing_metadata = await query_document_metadata(
                        doc_id=file_id, doc_type="file", user_id=user_id
                    )

                    # Send if never indexed or modified since last index
                    # Compare against stored modified_at (not indexed_at!)
                    needs_indexing = False
                    if existing_metadata is None:
                        # Never seen before
                        needs_indexing = True
                    elif existing_metadata.get("modified_at", 0) < modified_at:
                        # File modified since last indexing
                        needs_indexing = True
                    elif existing_metadata.get("is_placeholder", False):
                        # Placeholder exists - check if it's stale (processing may have failed)
                        # Only requeue if placeholder is older than 2x scan interval
                        queued_at = existing_metadata.get("queued_at", 0)
                        placeholder_age = time.time() - queued_at
                        stale_threshold = get_settings().vector_sync_scan_interval * 2
                        if placeholder_age > stale_threshold:
                            logger.debug(
                                f"Found stale placeholder for file {file_path} (ID: {file_id}) "
                                f"(age={placeholder_age:.1f}s), requeuing"
                            )
                            needs_indexing = True
                        else:
                            logger.debug(
                                f"Skipping file {file_path} (ID: {file_id}) with recent placeholder "
                                f"(age={placeholder_age:.1f}s < {stale_threshold:.1f}s)"
                            )

                    if needs_indexing:
                        # Write placeholder before queuing
                        await write_placeholder_point(
                            doc_id=file_id,
                            doc_type="file",
                            user_id=user_id,
                            modified_at=modified_at,
                            file_path=file_path,
                        )
                        await send_stream.send(
                            DocumentTask(
                                user_id=user_id,
                                doc_id=file_id,  # Use numeric file ID
                                doc_type="file",
                                operation="index",
                                modified_at=modified_at,
                                file_path=file_path,  # Pass file path for content retrieval
                            )
                        )
                        file_queued += 1

            logger.info(
                f"[SCAN-{scan_id}] Found {file_count} tagged PDFs for {user_id}"
            )
            record_vector_sync_scan(file_count)

            # Check for deleted files (not initial sync)
            if not initial_sync:
                for file_id in indexed_file_ids:
                    if file_id not in nextcloud_file_ids:
                        file_key = (user_id, file_id)

                        if file_key in _potentially_deleted:
                            # Check if grace period elapsed
                            first_missing_time = _potentially_deleted[file_key]
                            time_missing = current_time - first_missing_time

                            if time_missing >= grace_period:
                                # Grace period elapsed, send for deletion
                                logger.info(
                                    f"File ID {file_id} missing for {time_missing:.1f}s "
                                    f"(>{grace_period:.1f}s grace period), sending deletion"
                                )
                                await send_stream.send(
                                    DocumentTask(
                                        user_id=user_id,
                                        doc_id=file_id,  # Use numeric file ID
                                        doc_type="file",
                                        operation="delete",
                                        modified_at=0,
                                    )
                                )
                                file_queued += 1
                                del _potentially_deleted[file_key]
                        else:
                            # First time missing, add to grace period tracking
                            logger.debug(
                                f"File ID {file_id} missing for first time, starting grace period"
                            )
                            _potentially_deleted[file_key] = current_time

        except Exception as e:
            logger.warning(f"Failed to scan tagged files for {user_id}: {e}")

        queued += file_queued

        if queued > 0:
            logger.info(
                f"Sent {queued} documents ({file_queued} files) for incremental sync: {user_id}"
            )
        else:
            logger.debug(f"No changes detected for {user_id}")
