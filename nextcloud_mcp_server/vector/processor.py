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
from nextcloud_mcp_server.embedding import get_bm25_service, get_embedding_service
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


def assign_page_numbers(chunks, page_boundaries):
    """Assign page numbers to chunks based on page boundaries.

    Each chunk gets the page number where most of its content appears.
    For chunks spanning multiple pages, assigns the page containing the
    majority of the chunk's characters.

    Args:
        chunks: List of ChunkWithPosition objects
        page_boundaries: List of dicts with {page, start_offset, end_offset}

    Returns:
        None (modifies chunks in place)
    """
    if not page_boundaries:
        return

    for chunk in chunks:
        # Find which page(s) this chunk overlaps with
        max_overlap = 0
        assigned_page = None

        for boundary in page_boundaries:
            # Calculate overlap between chunk and page
            overlap_start = max(chunk.start_offset, boundary["start_offset"])
            overlap_end = min(chunk.end_offset, boundary["end_offset"])
            overlap = max(0, overlap_end - overlap_start)

            # Assign to page with maximum overlap
            if overlap > max_overlap:
                max_overlap = overlap
                assigned_page = boundary["page"]

        if assigned_page is not None:
            chunk.page_number = assigned_page


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
        file_metadata = {}  # No file-specific metadata for notes
        file_path = None  # Notes don't have file paths
    elif doc_task.doc_type == "file":
        # For files, doc_id is now the numeric file ID, file_path comes from DocumentTask
        if not doc_task.file_path:
            raise ValueError(
                f"File path required for file indexing but not provided (file_id={doc_task.doc_id})"
            )
        file_path = doc_task.file_path

        # Read file content via WebDAV
        content_bytes, content_type = await nc_client.webdav.read_file(file_path)

        # Use document processor registry to extract text
        from nextcloud_mcp_server.document_processors import get_registry

        registry = get_registry()

        try:
            result = await registry.process(
                content=content_bytes,
                content_type=content_type,
                filename=file_path,
            )
            content = result.text
            file_metadata = result.metadata
            title = file_metadata.get("title") or file_path.split("/")[-1]
            etag = ""  # WebDAV read_file doesn't return etag
        except Exception as e:
            logger.error(f"Failed to process file {file_path}: {e}")
            raise
    else:
        raise ValueError(f"Unsupported doc_type: {doc_task.doc_type}")

    # Tokenize and chunk (using configured chunk size and overlap)
    chunker = DocumentChunker(
        chunk_size=settings.document_chunk_size,
        overlap=settings.document_chunk_overlap,
    )
    chunks = await chunker.chunk_text(content)

    # Assign page numbers to chunks if page boundaries are available (PDFs)
    if doc_task.doc_type == "file" and "page_boundaries" in file_metadata:
        assign_page_numbers(chunks, file_metadata["page_boundaries"])

    # Extract chunk texts for embedding
    chunk_texts = [chunk.text for chunk in chunks]

    # Generate dense embeddings (I/O bound - external API call)
    embedding_service = get_embedding_service()
    dense_embeddings = await embedding_service.embed_batch(chunk_texts)

    # Generate sparse embeddings (BM25 for keyword matching)
    bm25_service = get_bm25_service()
    sparse_embeddings = await bm25_service.encode_batch(chunk_texts)

    # Prepare Qdrant points
    indexed_at = int(time.time())
    points = []

    for i, (chunk, dense_emb, sparse_emb) in enumerate(
        zip(chunks, dense_embeddings, sparse_embeddings)
    ):
        # Generate deterministic UUID for point ID
        # Using uuid5 with DNS namespace and combining doc info
        point_name = f"{doc_task.doc_type}:{doc_task.doc_id}:chunk:{i}"
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, point_name))

        points.append(
            PointStruct(
                id=point_id,
                vector={
                    "dense": dense_emb,
                    "sparse": sparse_emb,
                },
                payload={
                    "user_id": doc_task.user_id,
                    "doc_id": doc_task.doc_id,
                    "doc_type": doc_task.doc_type,
                    "title": title,
                    "excerpt": chunk.text[:200],
                    "indexed_at": indexed_at,
                    "modified_at": doc_task.modified_at,
                    "etag": etag,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "chunk_start_offset": chunk.start_offset,
                    "chunk_end_offset": chunk.end_offset,
                    "metadata_version": 2,  # v2 includes position metadata
                    # File-specific metadata (PDF, etc.)
                    **(
                        {
                            "file_path": file_path,  # Store file path for retrieval
                            "mime_type": file_metadata.get("content_type", ""),
                            "file_size": file_metadata.get("file_size"),
                            "page_number": chunk.page_number,
                            "page_count": file_metadata.get("page_count"),
                            "author": file_metadata.get("author"),
                            "creation_date": file_metadata.get("creation_date"),
                            "has_images": file_metadata.get("has_images", False),
                            "image_count": file_metadata.get("image_count", 0),
                        }
                        if doc_task.doc_type == "file"
                        else {}
                    ),
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
