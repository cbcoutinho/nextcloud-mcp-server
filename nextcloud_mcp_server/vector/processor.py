"""Processor task for vector database synchronization.

Processes documents from stream: fetches content, generates embeddings, stores in Qdrant.
"""

import logging
import time
import uuid

import anyio
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream
from httpx import HTTPStatusError
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.observability.metrics import (
    record_qdrant_operation,
    record_vector_sync_processing,
    update_vector_sync_queue_size,
)
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.vector.document_chunker import DocumentChunker
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client
from nextcloud_mcp_server.vector.scanner import DocumentTask

logger = logging.getLogger(__name__)


async def processor_task(
    worker_id: int,
    receive_stream: MemoryObjectReceiveStream[DocumentTask],
    shutdown_event: anyio.Event,
    nc_client: NextcloudClient,
    user_id: str,
    *,
    task_status: TaskStatus = anyio.TASK_STATUS_IGNORED,
):
    """
    Process documents from stream concurrently.

    Each processor task runs in a loop:
    1. Receive document from stream (with timeout)
    2. Fetch content from Nextcloud
    3. Tokenize and chunk text
    4. Generate embeddings (I/O bound - external API)
    5. Upload vectors to Qdrant

    Multiple processors run concurrently for I/O parallelism.

    Args:
        worker_id: Worker identifier for logging
        receive_stream: Stream to receive documents from
        shutdown_event: Event signaling shutdown
        nc_client: Authenticated Nextcloud client
        user_id: User being processed
        task_status: Status object for signaling task readiness
    """
    logger.info(f"Processor {worker_id} started")

    # Signal that the task has started and is ready
    task_status.started()

    while not shutdown_event.is_set():
        try:
            # Get document with timeout (allows checking shutdown)
            with anyio.fail_after(1.0):
                doc_task = await receive_stream.receive()

            # Update queue size metric after receiving
            stream_stats = receive_stream.statistics()
            update_vector_sync_queue_size(stream_stats.current_buffer_used)

            # Process document
            await process_document(doc_task, nc_client)

            # Update queue size metric after processing
            stream_stats = receive_stream.statistics()
            update_vector_sync_queue_size(stream_stats.current_buffer_used)

        except TimeoutError:
            # No documents available, update metric to show empty queue
            stream_stats = receive_stream.statistics()
            update_vector_sync_queue_size(stream_stats.current_buffer_used)
            continue

        except anyio.EndOfStream:
            # Scanner finished and closed stream, exit gracefully
            logger.info(f"Processor {worker_id}: Scanner finished, exiting")
            break

        except Exception as e:
            logger.error(
                f"Processor {worker_id} error processing "
                f"{doc_task.doc_type}_{doc_task.doc_id}: {e}",
                exc_info=True,
            )
            # Continue to next document (no task_done() needed with streams)

    logger.info(f"Processor {worker_id} stopped")


async def process_document(doc_task: DocumentTask, nc_client: NextcloudClient):
    """
    Process a single document: fetch, tokenize, embed, store in Qdrant.

    Implements retry logic with exponential backoff for transient failures.

    Args:
        doc_task: Document task to process
        nc_client: Authenticated Nextcloud client
    """
    start_time = time.time()

    logger.debug(
        f"Processing {doc_task.doc_type}_{doc_task.doc_id} "
        f"for {doc_task.user_id} ({doc_task.operation})"
    )

    with trace_operation(
        "vector_sync.process_document",
        attributes={
            "vector_sync.operation": "process",
            "vector_sync.user_id": doc_task.user_id,
            "vector_sync.doc_id": doc_task.doc_id,
            "vector_sync.doc_type": doc_task.doc_type,
            "vector_sync.doc_operation": doc_task.operation,
        },
    ):
        try:
            qdrant_client = await get_qdrant_client()
            settings = get_settings()

            # Handle deletion
            if doc_task.operation == "delete":
                await qdrant_client.delete(
                    collection_name=settings.get_collection_name(),
                    points_selector=Filter(
                        must=[
                            FieldCondition(
                                key="user_id",
                                match=MatchValue(value=doc_task.user_id),
                            ),
                            FieldCondition(
                                key="doc_id",
                                match=MatchValue(value=doc_task.doc_id),
                            ),
                            FieldCondition(
                                key="doc_type",
                                match=MatchValue(value=doc_task.doc_type),
                            ),
                        ]
                    ),
                )
                logger.info(
                    f"Deleted {doc_task.doc_type}_{doc_task.doc_id} for {doc_task.user_id}"
                )

                # Record successful deletion metrics
                duration = time.time() - start_time
                record_qdrant_operation("delete", "success")
                record_vector_sync_processing(duration, "success")
                return

            # Handle indexing with retry
            max_retries = 3
            retry_delay = 1.0

            for attempt in range(max_retries):
                try:
                    await _index_document(doc_task, nc_client, qdrant_client)

                    # Record successful processing metrics
                    duration = time.time() - start_time
                    record_qdrant_operation("upsert", "success")
                    record_vector_sync_processing(duration, "success")
                    return  # Success

                except (HTTPStatusError, Exception) as e:
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Retry {attempt + 1}/{max_retries} for "
                            f"{doc_task.doc_type}_{doc_task.doc_id}: {e}"
                        )
                        await anyio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        logger.error(
                            f"Failed to index {doc_task.doc_type}_{doc_task.doc_id} "
                            f"after {max_retries} retries: {e}"
                        )
                        # Record failed processing metrics
                        duration = time.time() - start_time
                        record_qdrant_operation("upsert", "error")
                        record_vector_sync_processing(duration, "error")
                        raise

        except Exception:
            # Catch any other unexpected errors
            duration = time.time() - start_time
            record_vector_sync_processing(duration, "error")
            raise


async def _index_document(
    doc_task: DocumentTask, nc_client: NextcloudClient, qdrant_client
):
    """
    Index a single document (called by process_document with retry).

    Args:
        doc_task: Document task to index
        nc_client: Authenticated Nextcloud client
        qdrant_client: Qdrant client instance
    """
    settings = get_settings()

    # Fetch document content
    if doc_task.doc_type == "note":
        document = await nc_client.notes.get_note(int(doc_task.doc_id))
        content = f"{document['title']}\n\n{document['content']}"
        title = document["title"]
        etag = document.get("etag", "")
    else:
        raise ValueError(f"Unsupported doc_type: {doc_task.doc_type}")

    # Tokenize and chunk (using configured chunk size and overlap)
    chunker = DocumentChunker(
        chunk_size=settings.document_chunk_size,
        overlap=settings.document_chunk_overlap,
    )
    chunks = chunker.chunk_text(content)

    # Generate embeddings (I/O bound - external API call)
    embedding_service = get_embedding_service()
    embeddings = await embedding_service.embed_batch(chunks)

    # Prepare Qdrant points
    indexed_at = int(time.time())
    points = []

    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        # Generate deterministic UUID for point ID
        # Using uuid5 with DNS namespace and combining doc info
        point_name = f"{doc_task.doc_type}:{doc_task.doc_id}:chunk:{i}"
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, point_name))

        points.append(
            PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "user_id": doc_task.user_id,
                    "doc_id": doc_task.doc_id,
                    "doc_type": doc_task.doc_type,
                    "title": title,
                    "excerpt": chunk[:200],
                    "indexed_at": indexed_at,
                    "modified_at": doc_task.modified_at,
                    "etag": etag,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                },
            )
        )

    # Upsert to Qdrant
    await qdrant_client.upsert(
        collection_name=settings.get_collection_name(),
        points=points,
        wait=True,
    )

    logger.info(
        f"Indexed {doc_task.doc_type}_{doc_task.doc_id} for {doc_task.user_id} "
        f"({len(chunks)} chunks)"
    )
