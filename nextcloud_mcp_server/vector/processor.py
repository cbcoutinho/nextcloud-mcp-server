"""Processor task for vector database synchronization.

Processes documents from queue: fetches content, generates embeddings, stores in Qdrant.
"""

import asyncio
import logging
import time
import uuid

import anyio
from httpx import HTTPStatusError
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.embedding import get_embedding_service
from nextcloud_mcp_server.vector.document_chunker import DocumentChunker
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client
from nextcloud_mcp_server.vector.scanner import DocumentTask

logger = logging.getLogger(__name__)


async def processor_task(
    worker_id: int,
    document_queue: asyncio.Queue,
    shutdown_event: anyio.Event,
    nc_client: NextcloudClient,
    user_id: str,
):
    """
    Process documents from queue concurrently.

    Each processor task runs in a loop:
    1. Pull document from queue (with timeout)
    2. Fetch content from Nextcloud
    3. Tokenize and chunk text
    4. Generate embeddings (I/O bound - external API)
    5. Upload vectors to Qdrant
    6. Mark task complete

    Multiple processors run concurrently for I/O parallelism.

    Args:
        worker_id: Worker identifier for logging
        document_queue: Queue to pull documents from
        shutdown_event: Event signaling shutdown
        nc_client: Authenticated Nextcloud client
        user_id: User being processed
    """
    logger.info(f"Processor {worker_id} started")

    while not shutdown_event.is_set():
        try:
            # Get document with timeout (allows checking shutdown)
            doc_task = await asyncio.wait_for(
                document_queue.get(),
                timeout=1.0,
            )

            # Process document
            await process_document(doc_task, nc_client)

            # Mark complete
            document_queue.task_done()

        except asyncio.TimeoutError:
            # No documents available, continue
            continue

        except Exception as e:
            logger.error(
                f"Processor {worker_id} error processing "
                f"{doc_task.doc_type}_{doc_task.doc_id}: {e}",
                exc_info=True,
            )
            # Mark task done even on error to prevent queue blocking
            try:
                document_queue.task_done()
            except ValueError:
                pass

    logger.info(f"Processor {worker_id} stopped")


async def process_document(doc_task: DocumentTask, nc_client: NextcloudClient):
    """
    Process a single document: fetch, tokenize, embed, store in Qdrant.

    Implements retry logic with exponential backoff for transient failures.

    Args:
        doc_task: Document task to process
        nc_client: Authenticated Nextcloud client
    """
    logger.debug(
        f"Processing {doc_task.doc_type}_{doc_task.doc_id} "
        f"for {doc_task.user_id} ({doc_task.operation})"
    )

    qdrant_client = await get_qdrant_client()
    settings = get_settings()

    # Handle deletion
    if doc_task.operation == "delete":
        await qdrant_client.delete(
            collection_name=settings.qdrant_collection,
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
        return

    # Handle indexing with retry
    max_retries = 3
    retry_delay = 1.0

    for attempt in range(max_retries):
        try:
            await _index_document(doc_task, nc_client, qdrant_client)
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

    # Tokenize and chunk
    chunker = DocumentChunker(chunk_size=512, overlap=50)
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
        collection_name=settings.qdrant_collection,
        points=points,
        wait=True,
    )

    logger.info(
        f"Indexed {doc_task.doc_type}_{doc_task.doc_id} for {doc_task.user_id} "
        f"({len(chunks)} chunks)"
    )
