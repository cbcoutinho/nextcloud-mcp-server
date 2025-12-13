"""Processor task for vector database synchronization.

Processes documents from stream: fetches content, generates embeddings, stores in Qdrant.
"""

import logging
import time
import uuid
from typing import Any, cast

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
from nextcloud_mcp_server.vector.placeholder import delete_placeholder_point
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
    with trace_operation(
        "vector_sync.fetch_content",
        attributes={
            "vector_sync.doc_type": doc_task.doc_type,
            "vector_sync.doc_id": doc_task.doc_id,
        },
    ):
        if doc_task.doc_type == "note":
            document = await nc_client.notes.get_note(int(doc_task.doc_id))
            content = f"{document['title']}\n\n{document['content']}"
            title = document["title"]
            etag = document.get("etag", "")
            file_metadata = {}  # No file-specific metadata for notes
            file_path = None  # Notes don't have file paths
            content_bytes = None  # Notes don't have binary content
            content_type = None
        elif doc_task.doc_type == "news_item":
            from nextcloud_mcp_server.vector.html_processor import html_to_markdown

            item = await nc_client.news.get_item(int(doc_task.doc_id))
            # Convert HTML body to Markdown for better embedding
            body_markdown = html_to_markdown(item.get("body", ""))
            # Build content: title + URL + body
            item_title = item.get("title", "")
            item_url = item.get("url", "")
            feed_title = item.get("feedTitle", "")

            # Structure content for embedding
            content_parts = [item_title]
            if feed_title:
                content_parts.append(f"Source: {feed_title}")
            if item_url:
                content_parts.append(f"URL: {item_url}")
            content_parts.append("")  # Blank line
            content_parts.append(body_markdown)
            content = "\n".join(content_parts)

            title = item_title
            etag = item.get("guidHash", "")
            # Store news-specific metadata for later use in payload
            file_metadata = {
                "feed_id": item.get("feedId"),
                "feed_title": feed_title,
                "author": item.get("author"),
                "pub_date": item.get("pubDate"),
                "starred": item.get("starred", False),
                "unread": item.get("unread", True),
                "url": item_url,
                "guid_hash": item.get("guidHash"),
                "enclosure_link": item.get("enclosureLink"),
                "enclosure_mime": item.get("enclosureMime"),
            }
            file_path = None
            content_bytes = None
            content_type = None
        elif doc_task.doc_type == "deck_card":
            # Fetch card from Deck API
            # Use metadata from scanner if available (O(1) lookup)
            # Otherwise fall back to iteration (legacy data)
            card = None
            board = None
            stack = None

            if (
                doc_task.metadata
                and "board_id" in doc_task.metadata
                and "stack_id" in doc_task.metadata
            ):
                # Fast path: Direct lookup with known board_id/stack_id
                board_id = doc_task.metadata["board_id"]
                stack_id = doc_task.metadata["stack_id"]
                try:
                    card = await nc_client.deck.get_card(
                        board_id=int(board_id),
                        stack_id=int(stack_id),
                        card_id=int(doc_task.doc_id),
                    )
                    # Fetch board and stack info for metadata
                    boards = await nc_client.deck.get_boards()
                    for b in boards:
                        if b.id == int(board_id):
                            board = b
                            stacks = await nc_client.deck.get_stacks(b.id)
                            for s in stacks:
                                if s.id == int(stack_id):
                                    stack = s
                                    break
                            break
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch card with metadata (board_id={board_id}, stack_id={stack_id}, card_id={doc_task.doc_id}): {e}, falling back to iteration"
                    )

            # Fallback: Iterate through all boards/stacks (for legacy data or if fast path failed)
            if card is None:
                boards = await nc_client.deck.get_boards()
                card_found = False

                for b in boards:
                    if card_found:
                        break
                    # Skip deleted boards (soft delete: deletedAt > 0)
                    if b.deletedAt > 0:
                        continue
                    stacks = await nc_client.deck.get_stacks(b.id)
                    for s in stacks:
                        if card_found:
                            break
                        if s.cards:
                            for c in s.cards:
                                if c.id == int(doc_task.doc_id):
                                    card = c
                                    board = b
                                    stack = s
                                    card_found = True
                                    break

                if not card_found:
                    raise ValueError(
                        f"Deck card {doc_task.doc_id} not found in any board/stack"
                    )

            # Build content from card title and description
            content_parts = [card.title]
            if card.description:
                content_parts.append(card.description)
            content = "\n\n".join(content_parts)
            title = card.title

            # Store deck-specific metadata
            file_metadata = {
                "board_id": board.id,
                "board_title": board.title,
                "stack_id": stack.id,
                "stack_title": stack.title,
                "card_type": card.type,
                "duedate": (card.duedate.isoformat() if card.duedate else None),
                "archived": card.archived,
                "owner": (
                    card.owner.uid if hasattr(card.owner, "uid") else str(card.owner)
                ),
            }
            etag = card.etag or ""
            file_path = None
            content_bytes = None
            content_type = None
        elif doc_task.doc_type == "file":
            # For files, doc_id is now the numeric file ID, file_path comes from DocumentTask
            if not doc_task.file_path:
                raise ValueError(
                    f"File path required for file indexing but not provided (file_id={doc_task.doc_id})"
                )
            file_path = doc_task.file_path

            # Read file content via WebDAV
            content_bytes, content_type = await nc_client.webdav.read_file(file_path)
        else:
            raise ValueError(f"Unsupported doc_type: {doc_task.doc_type}")

    # Process file content (text extraction)
    if doc_task.doc_type == "file":
        # Type narrowing: content_bytes and content_type are set for files
        assert content_bytes is not None
        assert content_type is not None
        assert file_path is not None

        with trace_operation(
            "vector_sync.document_process",
            attributes={
                "vector_sync.content_type": content_type,
                "vector_sync.file_size": len(content_bytes),
            },
        ):
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

                # Diagnostic: Log page boundary information if available
                if "page_boundaries" in file_metadata:
                    page_boundaries = file_metadata["page_boundaries"]
                    logger.info(
                        f"Page boundaries for {file_path}: "
                        f"{len(page_boundaries)} pages, text length: {len(content)}"
                    )
                    # Log first 3 page boundaries for debugging
                    for boundary in page_boundaries[:3]:
                        logger.debug(
                            f"  Page {boundary['page']}: "
                            f"offsets [{boundary['start_offset']}:{boundary['end_offset']}]"
                        )
                    # Verify last boundary matches text length
                    if page_boundaries:
                        last_boundary = page_boundaries[-1]
                        if last_boundary["end_offset"] != len(content):
                            logger.warning(
                                f"Text length mismatch: content={len(content)}, "
                                f"last_boundary_end={last_boundary['end_offset']}"
                            )
                else:
                    logger.debug(f"No page_boundaries in metadata for {file_path}")
            except Exception as e:
                logger.error(f"Failed to process file {file_path}: {e}")
                raise

    # Tokenize and chunk (using configured chunk size and overlap)
    with trace_operation(
        "vector_sync.chunk_text",
        attributes={
            "vector_sync.input_chars": len(content),
            "vector_sync.chunk_size": settings.document_chunk_size,
            "vector_sync.overlap": settings.document_chunk_overlap,
        },
    ):
        chunker = DocumentChunker(
            chunk_size=settings.document_chunk_size,
            overlap=settings.document_chunk_overlap,
        )
        chunks = await chunker.chunk_text(content)

    # Assign page numbers to chunks if page boundaries are available (PDFs)
    page_boundaries = file_metadata.get("page_boundaries")
    if doc_task.doc_type == "file" and page_boundaries is not None:
        # Type narrowing: page_boundaries is guaranteed to be list[dict] here
        page_boundaries_list = cast(list[dict[str, Any]], page_boundaries)
        with trace_operation(
            "vector_sync.assign_page_numbers",
            attributes={
                "vector_sync.chunk_count": len(chunks),
                "vector_sync.page_count": len(page_boundaries_list),
            },
        ):
            assign_page_numbers(chunks, page_boundaries_list)

            # Diagnostic: Verify page number assignment
            assigned_count = sum(1 for c in chunks if c.page_number is not None)
            logger.info(
                f"Assigned page numbers to {assigned_count}/{len(chunks)} chunks "
                f"for {file_path}"
            )

            # Log first 3 chunks to see their page assignments
            for i, chunk in enumerate(chunks[:3]):
                logger.debug(
                    f"  Chunk {i}: page={chunk.page_number}, "
                    f"offsets=[{chunk.start_offset}:{chunk.end_offset}]"
                )

            # Warning if NO page numbers were assigned
            if assigned_count == 0:
                logger.warning(
                    f"NO page numbers assigned! "
                    f"Text length: {len(content)}, "
                    f"Chunks: {len(chunks)}, "
                    f"Chunk offset range: [{chunks[0].start_offset}:{chunks[-1].end_offset}], "
                    f"Page boundaries: {len(page_boundaries_list)} pages, "
                    f"First boundary: {page_boundaries_list[0] if page_boundaries_list else 'None'}"
                )

    # Extract chunk texts for embedding
    chunk_texts = [chunk.text for chunk in chunks]

    # Initialize results containers
    dense_embeddings: list = []
    sparse_embeddings: list = []
    chunk_images: dict[int, dict] = {}

    # Determine if we need PDF highlighting
    is_pdf = doc_task.doc_type == "file" and content_type == "application/pdf"

    # Define async tasks for parallel execution
    async def generate_dense_embeddings():
        """Generate dense embeddings (I/O bound - external API call)."""
        nonlocal dense_embeddings
        with trace_operation(
            "vector_sync.embed_dense",
            attributes={
                "vector_sync.chunk_count": len(chunk_texts),
                "vector_sync.total_chars": sum(len(t) for t in chunk_texts),
            },
        ):
            embedding_service = get_embedding_service()
            dense_embeddings = await embedding_service.embed_batch(chunk_texts)

    async def generate_sparse_embeddings():
        """Generate sparse embeddings (BM25 for keyword matching)."""
        nonlocal sparse_embeddings
        with trace_operation(
            "vector_sync.embed_sparse",
            attributes={
                "vector_sync.chunk_count": len(chunk_texts),
            },
        ):
            bm25_service = get_bm25_service()
            sparse_embeddings = await bm25_service.encode_batch(chunk_texts)

    async def generate_highlights():
        """Generate highlighted page images for PDF chunks (CPU-bound)."""
        nonlocal chunk_images
        if not is_pdf:
            return

        # Type narrowing: content_bytes is set for PDF files
        assert content_bytes is not None

        with trace_operation(
            "vector_sync.generate_highlights",
            attributes={
                "vector_sync.chunk_count": len(chunks),
                "vector_sync.pdf_size": len(content_bytes),
            },
        ):
            import base64

            from nextcloud_mcp_server.search.pdf_highlighter import PDFHighlighter

            # Build chunk data for batch processing
            # Format: (chunk_index, start_offset, end_offset, page_number, chunk_text)
            chunk_data: list[tuple[int, int, int, int | None, str]] = [
                (i, chunk.start_offset, chunk.end_offset, chunk.page_number, chunk.text)
                for i, chunk in enumerate(chunks)
                if chunk.page_number is not None
            ]

            # Get pre-computed page boundaries from document processor
            page_boundaries = file_metadata.get("page_boundaries")
            if not page_boundaries:
                logger.warning("No page boundaries available, skipping highlighting")
                return

            # Type narrowing: page_boundaries is guaranteed to be list[dict] here
            page_boundaries_list = cast(list[dict[str, Any]], page_boundaries)

            logger.info(
                f"Batch generating highlighted page images for {len(chunk_data)} PDF chunks"
            )

            # Run CPU-bound highlighting in thread pool
            # Pass pre-computed page boundaries and full text to avoid re-processing the PDF
            batch_results = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                lambda: PDFHighlighter.highlight_chunks_batch(
                    pdf_bytes=content_bytes,
                    chunks=chunk_data,
                    page_boundaries=page_boundaries_list,
                    full_text=content,
                    color="yellow",
                    zoom=2.0,
                )
            )

            # Convert results to storage format
            for chunk_index, (
                png_bytes,
                actual_page_num,
                highlight_count,
            ) in batch_results.items():
                image_base64 = base64.b64encode(png_bytes).decode("utf-8")
                chunk_images[chunk_index] = {
                    "image": image_base64,
                    "page": actual_page_num,
                    "highlights": highlight_count,
                    "size": len(png_bytes),
                }

            logger.info(
                f"Generated {len(chunk_images)}/{len(chunks)} highlighted page images "
                f"(avg {sum(img['size'] for img in chunk_images.values()) // max(len(chunk_images), 1):,} bytes)"
            )

    # Run all embedding/highlighting operations in parallel
    # - Dense embeddings: I/O bound (API call)
    # - Sparse embeddings: CPU bound (local BM25)
    # - Highlighting: CPU bound (PyMuPDF rendering, runs in thread pool)
    with trace_operation(
        "vector_sync.parallel_processing",
        attributes={
            "vector_sync.is_pdf": is_pdf,
            "vector_sync.chunk_count": len(chunks),
        },
    ):
        async with anyio.create_task_group() as tg:
            tg.start_soon(generate_dense_embeddings)
            tg.start_soon(generate_sparse_embeddings)
            tg.start_soon(generate_highlights)

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
                    "is_placeholder": False,  # Real indexed document (not placeholder)
                    "title": title,
                    "excerpt": chunk.text,  # Full chunk text (up to chunk_size, default 2048 chars)
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
                            "mime_type": content_type,  # From WebDAV response
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
                    # News item-specific metadata
                    **(
                        {
                            "feed_id": file_metadata.get("feed_id"),
                            "feed_title": file_metadata.get("feed_title"),
                            "author": file_metadata.get("author"),
                            "pub_date": file_metadata.get("pub_date"),
                            "starred": file_metadata.get("starred"),
                            "unread": file_metadata.get("unread"),
                            "url": file_metadata.get("url"),
                            "guid_hash": file_metadata.get("guid_hash"),
                            "enclosure_link": file_metadata.get("enclosure_link"),
                            "enclosure_mime": file_metadata.get("enclosure_mime"),
                        }
                        if doc_task.doc_type == "news_item"
                        else {}
                    ),
                    # Deck card-specific metadata
                    **(
                        {
                            "board_id": file_metadata.get("board_id"),
                            "board_title": file_metadata.get("board_title"),
                            "stack_id": file_metadata.get("stack_id"),
                            "stack_title": file_metadata.get("stack_title"),
                            "card_type": file_metadata.get("card_type"),
                            "duedate": file_metadata.get("duedate"),
                            "owner": file_metadata.get("owner"),
                        }
                        if doc_task.doc_type == "deck_card"
                        else {}
                    ),
                    # Highlighted page image (PDF only)
                    **(
                        {
                            "highlighted_page_image": chunk_images[i]["image"],
                            "highlighted_page_number": chunk_images[i]["page"],
                            "highlight_count": chunk_images[i]["highlights"],
                        }
                        if i in chunk_images
                        else {}
                    ),
                },
            )
        )

    # Delete placeholder before writing real vectors
    # This prevents duplicates and cleans up the placeholder state
    try:
        await delete_placeholder_point(
            doc_id=doc_task.doc_id,
            doc_type=doc_task.doc_type,
            user_id=doc_task.user_id,
        )
    except Exception as e:
        # Log but don't fail indexing if placeholder deletion fails
        logger.warning(
            f"Failed to delete placeholder for {doc_task.doc_type}_{doc_task.doc_id}: {e}"
        )

    # Upsert to Qdrant in batches to avoid timeout with large payloads
    # Each batch is limited to avoid WriteTimeout when sending large image payloads
    BATCH_SIZE = 10  # ~2MB per batch with images
    with trace_operation(
        "vector_sync.qdrant_upsert",
        attributes={
            "vector_sync.point_count": len(points),
            "vector_sync.collection": settings.get_collection_name(),
            "vector_sync.images_count": len(chunk_images),
            "vector_sync.batch_size": BATCH_SIZE,
        },
    ):
        for batch_start in range(0, len(points), BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, len(points))
            batch = points[batch_start:batch_end]
            await qdrant_client.upsert(
                collection_name=settings.get_collection_name(),
                points=batch,
                wait=True,
            )
            if batch_end < len(points):
                logger.debug(
                    f"Upserted batch {batch_start // BATCH_SIZE + 1}/{(len(points) + BATCH_SIZE - 1) // BATCH_SIZE}"
                )

    logger.info(
        f"Indexed {doc_task.doc_type}_{doc_task.doc_id} for {doc_task.user_id} "
        f"({len(chunks)} chunks)"
    )
