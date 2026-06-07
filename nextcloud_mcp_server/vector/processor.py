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
from qdrant_client.models import PointStruct

from nextcloud_mcp_server.acl_hash import compute_acl_hash
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.document_processors import get_registry
from nextcloud_mcp_server.embedding import get_bm25_service, get_embedding_service
from nextcloud_mcp_server.models.deck import DeckCard
from nextcloud_mcp_server.observability.metrics import (
    record_document_chunks,
    record_document_parse_failed,
    record_embedding,
    record_qdrant_operation,
    record_vector_sync_processing,
    update_vector_sync_queue_size,
)
from nextcloud_mcp_server.observability.tracing import trace_operation
from nextcloud_mcp_server.search.pdf_highlighter import PDFHighlighter
from nextcloud_mcp_server.usage import UsageEventStore
from nextcloud_mcp_server.vector import payload_keys
from nextcloud_mcp_server.vector.document_chunker import (
    DocumentChunker,
    PageAwareChunker,
)
from nextcloud_mcp_server.vector.html_processor import html_to_markdown
from nextcloud_mcp_server.vector.placeholder import (
    delete_placeholder_point,
    update_placeholder_status,
)
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client
from nextcloud_mcp_server.vector.scanner import DocumentTask
from nextcloud_mcp_server.vector.sharing_state import (
    claim_existing_index,
    existing_principals,
    file_title_from_path,
    release_document_for_user,
)

logger = logging.getLogger(__name__)

# Shared span-attribute key (avoids duplicating the string literal across the
# many vector_sync spans that report a chunk count).
_ATTR_CHUNK_COUNT = "vector_sync.chunk_count"


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


def should_use_page_aware(
    *, page_aware_enabled: bool, doc_type: str, page_boundaries: Any
) -> bool:
    """Decide whether the page-aware chunker applies to this document.

    Page-aware chunking applies only to paginated files (PDFs) that actually
    carry page boundaries. ``page_boundaries`` is tested for truthiness, not
    just ``is not None``: an empty list carries no pages, so it routes through
    the char-based path rather than the page-aware chunker's no-boundaries
    fallback.

    Args:
        page_aware_enabled: ``settings.document_chunk_page_aware``.
        doc_type: The document type (only ``"file"`` is paginated).
        page_boundaries: The extractor's page-boundary list (or ``None``).
    """
    return page_aware_enabled and doc_type == "file" and bool(page_boundaries)


async def record_indexing_usage(
    *,
    enabled: bool,
    provider: str,
    model: str,
    doc_type: str,
    user_id: str,
    chunk_count: int,
    token_count: int,
    total_chars: int,
) -> None:
    """Record the two billable usage events for one embedded document.

    ``pages_chunks`` is the volume (chunks embedded); ``embeddings_queries`` is
    the embedding request's token count — the same metric search records, so the
    meter bills embedding tokens whether they were incurred indexing a document
    or embedding a query (Deck #67).

    Best-effort and flag-gated: a metering failure is logged and never breaks
    indexing. No-op when metering is disabled or the document produced no chunks
    (an empty batch embeds nothing and would only write zero-value rows).

    Privacy note: ``user_id`` stays tenant-local — the CP rollup aggregates
    GROUP BY (day, metric) into ``usage_daily`` (no metadata column), so nothing
    here reaches Stripe; it is retained only to keep Deck #67's future per-user
    attribution derivable from the app DB without a re-migration.
    """
    if not enabled or chunk_count == 0:
        return

    metadata = {
        "provider": provider,
        "model": model,
        "doc_type": doc_type,
        "user_id": user_id,
        "total_chars": total_chars,
    }
    try:
        store = await UsageEventStore.shared()
        # enabled=True: the guard above already confirmed the flag, so the store
        # skips a second uncached Settings build per record (ADR-024).
        # record_usage_event swallows its own write failures, so the two records
        # are independent; if pages_chunks somehow raised mid-way, embeddings_-
        # queries would be skipped, leaving an unmatched pages_chunks row —
        # acceptable under the (day, metric) SUM-aggregation billing model.
        await store.record_usage_event(
            metric="pages_chunks", value=chunk_count, metadata=metadata, enabled=True
        )
        await store.record_usage_event(
            metric="embeddings_queries",
            value=token_count,
            metadata=metadata,
            enabled=True,
        )
    except Exception:
        # Reached only when shared()/store construction itself raises
        # (record_usage_event swallows its own write failures). Metering is on,
        # so warn rather than hide the "enabled but no billing data" case.
        logger.warning(
            "usage metering hook (indexing embeddings) skipped", exc_info=True
        )


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
    logger.info("Processor %s started", worker_id)

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
            logger.info("Processor %s: Scanner finished, exiting", worker_id)
            break

        except Exception as e:
            logger.error(
                "Processor %s error processing %s_%s: %s",
                worker_id,
                doc_task.doc_type,
                doc_task.doc_id,
                e,
                exc_info=True,
            )
            # Continue to next document (no task_done() needed with streams)

    logger.info("Processor %s stopped", worker_id)


async def _reconcile_tag_event(
    doc_task: DocumentTask, nc_client: NextcloudClient
) -> None:
    """Resolve a tag-webhook file task into a concrete index or delete.

    A SystemTag ``MapperEvent`` only tells us a fileid's tags changed — not the
    path, nor whether our ``vector-index`` tag is (still) on it. Look up the
    user's current ``vector-index`` PDFs (the same call the scanner uses, which
    also expands tagged folders into their PDF descendants) and reconcile the
    task in place:

    - fileid present -> index it; fill path/etag/mtime from the tag listing.
    - fileid absent  -> it isn't a tagged PDF (anymore); flip ``operation`` to
      ``delete`` so any existing points are released for this user.

    A tagged *folder*'s own fileid won't appear in the file-level listing, so it
    resolves to a harmless no-op delete here; the hourly scanner still expands
    tagged folders into their descendants.
    """
    tag_name = get_settings().vector_sync_pdf_tag
    tagged = await nc_client.find_files_by_tag(
        tag_name, mime_type_filter="application/pdf"
    )
    match = next(
        (f for f in tagged if str(f.get("id")) == str(doc_task.doc_id)),
        None,
    )

    if match is None:
        doc_task.operation = "delete"
        logger.info(
            "Tag reconcile: file %s is not a %r PDF; releasing for %s",
            doc_task.doc_id,
            tag_name,
            doc_task.user_id,
        )
        return

    doc_task.file_path = match["path"]
    if not doc_task.etag:
        doc_task.etag = match.get("etag")
    last_modified = match.get("last_modified_timestamp")
    if last_modified:
        doc_task.modified_at = int(last_modified)
    logger.info(
        "Tag reconcile: indexing %s (file %s) for %s",
        doc_task.file_path,
        doc_task.doc_id,
        doc_task.user_id,
    )


async def process_document(
    doc_task: DocumentTask, nc_client: NextcloudClient, *, max_retries: int = 3
):
    """
    Process a single document: fetch, tokenize, embed, store in Qdrant.

    Implements retry logic with exponential backoff for transient failures.

    Args:
        doc_task: Document task to process
        nc_client: Authenticated Nextcloud client
        max_retries: In-process indexing attempts before re-raising. The default
            (3) suits the in-process SQLite pool, which has no durable retry. The
            procrastinate worker passes ``1`` so durable retry is owned by the
            queue (and survives worker crashes), avoiding compounding 3×N retries.
    """
    start_time = time.time()

    logger.debug(
        "Processing %s_%s for %s (%s)",
        doc_task.doc_type,
        doc_task.doc_id,
        doc_task.user_id,
        doc_task.operation,
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

            # Tag-webhook reconcile: a SystemTag MapperEvent enqueues a file task
            # carrying only a fileid (file_path is None — see
            # webhook_parser._parse_tag_event). Resolve the file's current
            # vector-index membership into a concrete index (path/etag filled) or
            # a delete before dispatching below.
            if (
                doc_task.doc_type == "file"
                and doc_task.operation == "index"
                and doc_task.file_path is None
            ):
                await _reconcile_tag_event(doc_task, nc_client)

            # Handle deletion
            if doc_task.operation == "delete":
                # Release this user rather than blind-delete: a file shared across
                # users has one user-agnostic point set referenced by multiple
                # principals, so the points are removed only once the last reader
                # is gone (see vector/sharing_state.release_document_for_user).
                await release_document_for_user(
                    doc_task.doc_id, doc_task.doc_type, doc_task.user_id
                )
                logger.info(
                    "Deleted %s_%s for %s",
                    doc_task.doc_type,
                    doc_task.doc_id,
                    doc_task.user_id,
                    extra={
                        "doc_id": doc_task.doc_id,
                        "doc_type": doc_task.doc_type,
                        "status": "success",
                    },
                )

                # Record successful deletion metrics. A delete is not an
                # indexing event, so doc_type is intentionally omitted here to
                # keep it out of astrolabe_documents_indexed_total.
                duration = time.time() - start_time
                record_qdrant_operation("delete", "success")
                record_vector_sync_processing(duration, "success")
                return

            # Handle indexing with retry
            retry_delay = 1.0

            for attempt in range(max_retries):
                try:
                    indexed = await _index_document(doc_task, nc_client, qdrant_client)

                    # A permanent parse failure returns False: it was already
                    # recorded (document_parse_failed_total + the registry's
                    # document_parse_total{error}) and the placeholder marked
                    # "failed". It is not an indexing event and not retryable, so
                    # don't count it as a successful upsert/indexed document.
                    # Identity check, not `if not indexed`: a successful index
                    # (including a dedup hit) returns None, which must NOT be
                    # treated as a parse failure.
                    if indexed is False:
                        return

                    # Record successful processing metrics
                    duration = time.time() - start_time
                    record_qdrant_operation("upsert", "success")
                    record_vector_sync_processing(
                        duration, "success", doc_type=doc_task.doc_type
                    )
                    return  # Success

                except Exception as e:
                    if attempt < max_retries - 1:
                        logger.warning(
                            "Retry %s/%s for %s_%s: %s",
                            attempt + 1,
                            max_retries,
                            doc_task.doc_type,
                            doc_task.doc_id,
                            e,
                            extra={
                                "doc_id": doc_task.doc_id,
                                "doc_type": doc_task.doc_type,
                                "attempt": attempt + 1,
                                "max_retries": max_retries,
                                "status": "retry",
                            },
                        )
                        await anyio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        logger.error(
                            "Failed to index %s_%s after %s retries: %s",
                            doc_task.doc_type,
                            doc_task.doc_id,
                            max_retries,
                            e,
                            extra={
                                "doc_id": doc_task.doc_id,
                                "doc_type": doc_task.doc_type,
                                "attempt": max_retries,
                                "max_retries": max_retries,
                                "status": "error",
                            },
                        )
                        # Record the failed Qdrant upsert. The processing-error
                        # metric is recorded once by the outer handler below, so
                        # exhausted-retry failures aren't double-counted.
                        record_qdrant_operation("upsert", "error")
                        raise

        except Exception:
            # Single processing-error call site: catches exhausted-retry
            # re-raises, delete failures, and setup errors (get_qdrant_client /
            # get_settings) — each counted exactly once. A failed delete is not
            # an indexing event either, so doc_type is omitted for deletes to
            # keep them out of astrolabe_documents_indexed_total.
            duration = time.time() - start_time
            indexed_doc_type = (
                None if doc_task.operation == "delete" else doc_task.doc_type
            )
            record_vector_sync_processing(duration, "error", doc_type=indexed_doc_type)
            raise


async def _index_document(
    doc_task: DocumentTask, nc_client: NextcloudClient, qdrant_client
) -> bool | None:
    """
    Index a single document (called by process_document with retry).

    Returns ``False`` when a permanent parse failure means nothing was indexed
    (the caller must then skip the success metrics); ``None`` otherwise.

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
                        "Failed to fetch card with metadata (board_id=%s, stack_id=%s, card_id=%s): %s, falling back to iteration",
                        board_id,
                        stack_id,
                        doc_task.doc_id,
                        e,
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
                            # get_stacks() always yields full DeckCard objects;
                            # the DeckCardSummary projection only happens in the
                            # tool layer, never on freshly-fetched stacks.
                            for c in cast(list[DeckCard], s.cards):
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

            # Type narrowing: card, board, stack are all set if we reach here
            assert card is not None
            assert board is not None
            assert stack is not None

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

            # Cross-worker dedup race-guard: two users' tasks for the same shared
            # file can be enqueued before either finishes. If another worker has
            # already indexed this exact content (fileid + etag + embedding model)
            # in the tenant, claim it for this user (observed-access ACL) and skip
            # the expensive fetch/parse/embed entirely.
            if doc_task.etag and await claim_existing_index(
                doc_task.doc_id,
                "file",
                doc_task.etag,
                doc_task.user_id,
                current_path=doc_task.file_path,
            ):
                await delete_placeholder_point(
                    doc_id=doc_task.doc_id,
                    doc_type="file",
                    user_id=doc_task.user_id,
                )
                # No embedding ran, so no usage is recorded here — stated
                # explicitly so a "fewer embeddings_queries rows than expected"
                # audit lands on the dedup path rather than reconstructing it
                # from Qdrant claim logs.
                logger.info(
                    "Dedup hit for file %s (etag=%s); claimed for user %s "
                    "without reprocessing (no embedding/usage recorded)",
                    doc_task.doc_id,
                    doc_task.etag,
                    doc_task.user_id,
                )
                return

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
            # The registry runs the tiered PDF pipeline (tier-0 classify ->
            # tier-1 fast -> OCR escalation) and records classification metrics.
            registry = get_registry()

            try:
                result = await registry.process(
                    content=content_bytes,
                    content_type=content_type,
                    filename=file_path,
                )

                # A permanent parse failure (e.g. an isolated-worker OOM/timeout
                # on a pathological PDF) returns success=False rather than
                # raising -- there is nothing to index and retrying would just
                # fail again. Mark the placeholder "failed" so the scanner stops
                # re-queuing it (until the file changes) and return False so the
                # caller skips the success metrics (it was not indexed).
                if not result.success:
                    reason = result.metadata.get("parse_failed_reason", "error")
                    record_document_parse_failed(reason)
                    logger.warning(
                        "Permanent parse failure for %s (reason=%s); marking "
                        "failed and skipping index",
                        file_path,
                        reason,
                    )
                    try:
                        await update_placeholder_status(
                            doc_id=doc_task.doc_id,
                            doc_type=doc_task.doc_type,
                            user_id=doc_task.user_id,
                            status="failed",
                        )
                    except Exception:
                        # Best-effort: a transient Qdrant error here only means
                        # the placeholder isn't marked, so the scanner retries
                        # the (still un-indexable) file later -- not fatal.
                        logger.debug(
                            "Could not mark placeholder failed for %s",
                            doc_task.doc_id,
                            exc_info=True,
                        )
                    return False

                content = result.text
                file_metadata = result.metadata
                # Favour the Nextcloud filename over any embedded document title
                # (e.g. a PDF's /Title), which often disagrees with how the user
                # named the file and is confusing in the UI.
                title = file_title_from_path(file_path)
                # etag comes from the scanner's tag REPORT (threaded via the
                # DocumentTask); read_file itself returns no etag. It is the
                # tenant-wide content-dedup key, so it must be persisted.
                etag = doc_task.etag or ""

                # Diagnostic: Log page boundary information if available
                if "page_boundaries" in file_metadata:
                    page_boundaries = file_metadata["page_boundaries"]
                    logger.debug(
                        "Page boundaries for %s: %s pages, text length: %s",
                        file_path,
                        len(page_boundaries),
                        len(content),
                    )
                    # Verify last boundary matches text length
                    if page_boundaries:
                        last_boundary = page_boundaries[-1]
                        if last_boundary["end_offset"] != len(content):
                            logger.warning(
                                "Text length mismatch: content=%s, last_boundary_end=%s",
                                len(content),
                                last_boundary["end_offset"],
                            )
                else:
                    logger.debug("No page_boundaries in metadata for %s", file_path)
            except Exception as e:
                logger.error("Failed to process file %s: %s", file_path, e)
                raise

    # Tokenize and chunk (using configured chunk size and overlap). Paginated
    # files (PDFs with page_boundaries) use the page-aware chunker when enabled,
    # which assigns page numbers inline; everything else uses the char-based
    # chunker followed by post-hoc page assignment.
    page_boundaries = file_metadata.get("page_boundaries")
    use_page_aware = should_use_page_aware(
        page_aware_enabled=settings.document_chunk_page_aware,
        doc_type=doc_task.doc_type,
        page_boundaries=page_boundaries,
    )
    with trace_operation(
        "vector_sync.chunk_text",
        attributes={
            "vector_sync.input_chars": len(content),
            "vector_sync.chunk_size": settings.document_chunk_size,
            "vector_sync.overlap": settings.document_chunk_overlap,
            "vector_sync.page_aware": use_page_aware,
        },
    ) as chunk_span:
        if use_page_aware:
            page_boundaries_list = cast(list[dict[str, Any]], page_boundaries)
            chunks = await PageAwareChunker(
                chunk_size=settings.document_chunk_size,
                overlap=settings.document_chunk_overlap,
            ).chunk_text(content, page_boundaries_list)
        else:
            chunks = await DocumentChunker(
                chunk_size=settings.document_chunk_size,
                overlap=settings.document_chunk_overlap,
            ).chunk_text(content)
        record_document_chunks(doc_task.doc_type, len(chunks))
        if chunk_span is not None:
            chunk_span.set_attribute(_ATTR_CHUNK_COUNT, len(chunks))

    # Assign page numbers for the char-based path (page-aware already sets them).
    # Truthy guard (not "is not None"): an empty boundary list has nothing to
    # assign, so skip the span and the "NO page numbers assigned" warning.
    if not use_page_aware and doc_task.doc_type == "file" and page_boundaries:
        # Type narrowing: page_boundaries is guaranteed to be list[dict] here
        page_boundaries_list = cast(list[dict[str, Any]], page_boundaries)
        with trace_operation(
            "vector_sync.assign_page_numbers",
            attributes={
                _ATTR_CHUNK_COUNT: len(chunks),
                "vector_sync.page_count": len(page_boundaries_list),
            },
        ):
            assign_page_numbers(chunks, page_boundaries_list)

            # Diagnostic: Verify page number assignment
            assigned_count = sum(1 for c in chunks if c.page_number is not None)
            logger.debug(
                "Assigned page numbers to %s/%s chunks for %s",
                assigned_count,
                len(chunks),
                file_path,
            )

            # Warning if NO page numbers were assigned
            if assigned_count == 0:
                logger.warning(
                    "NO page numbers assigned! Text length: %s, Chunks: %s, Chunk offset range: [%s:%s], Page boundaries: %s pages, First boundary: %s",
                    len(content),
                    len(chunks),
                    chunks[0].start_offset,
                    chunks[-1].end_offset,
                    len(page_boundaries_list),
                    page_boundaries_list[0] if page_boundaries_list else "None",
                )

    # Extract chunk texts for embedding
    chunk_texts = [chunk.text for chunk in chunks]

    # Initialize results containers
    dense_embeddings: list = []
    sparse_embeddings: list = []
    # chunk_index -> list[(x0, y0, x1, y1)] of normalized rectangles
    # in [0, 1] relative to page width/height. The page is taken from
    # `chunk.page_number` (offset-based) and stored as `page_number`
    # in the Qdrant payload, so we don't carry an `actual_page_num` here.
    chunk_bboxes: dict[int, list[tuple[float, float, float, float]]] = {}

    # Determine if we need PDF highlighting
    is_pdf = doc_task.doc_type == "file" and content_type == "application/pdf"

    # Define async tasks for parallel execution
    async def generate_dense_embeddings():
        """Generate dense embeddings (I/O bound - external API call)."""
        nonlocal dense_embeddings
        provider = settings.get_embedding_provider_family()
        total_chars = sum(len(t) for t in chunk_texts)
        with trace_operation(
            "vector_sync.embed_dense",
            attributes={
                _ATTR_CHUNK_COUNT: len(chunk_texts),
                "vector_sync.total_chars": total_chars,
                "embedding.kind": "dense",
                "embedding.provider": provider,
                "embedding.model": settings.get_embedding_model_name(),
            },
        ):
            embedding_service = get_embedding_service()
            embed_start = time.time()
            try:
                (
                    dense_embeddings,
                    embed_tokens,
                ) = await embedding_service.embed_batch_with_usage(chunk_texts)
            except Exception:
                record_embedding(
                    "dense", provider, time.time() - embed_start, status="error"
                )
                raise
            record_embedding(
                "dense",
                provider,
                time.time() - embed_start,
                chunks=len(chunk_texts),
                chars=total_chars,
            )
            # Usage metering (Deck #67): record the chunk volume +
            # embedding-token count for this document. Best-effort and
            # flag-gated; placed after the embedding succeeds so it can never
            # affect the indexing path. See record_indexing_usage for the
            # metric/privacy details.
            await record_indexing_usage(
                enabled=settings.usage_metering_enabled,
                provider=provider,
                model=settings.get_embedding_model_name(),
                doc_type=doc_task.doc_type,
                user_id=doc_task.user_id,
                chunk_count=len(chunk_texts),
                token_count=embed_tokens,
                total_chars=total_chars,
            )

    async def generate_sparse_embeddings():
        """Generate sparse embeddings (BM25 for keyword matching)."""
        nonlocal sparse_embeddings
        total_chars = sum(len(t) for t in chunk_texts)
        with trace_operation(
            "vector_sync.embed_sparse",
            attributes={
                _ATTR_CHUNK_COUNT: len(chunk_texts),
                "vector_sync.total_chars": total_chars,
                "embedding.kind": "sparse",
                "embedding.provider": "bm25",
            },
        ):
            bm25_service = await get_bm25_service()
            embed_start = time.time()
            try:
                sparse_embeddings = await bm25_service.encode_batch(chunk_texts)
            except Exception:
                record_embedding(
                    "sparse", "bm25", time.time() - embed_start, status="error"
                )
                raise
            record_embedding(
                "sparse",
                "bm25",
                time.time() - embed_start,
                chunks=len(chunk_texts),
                chars=total_chars,
            )

    async def generate_highlights():
        """Compute chunk bounding boxes for PDF chunks (CPU-bound, no rendering)."""
        nonlocal chunk_bboxes
        if not is_pdf:
            return

        # Type narrowing: content_bytes is set for PDF files
        assert content_bytes is not None

        with trace_operation(
            "vector_sync.compute_chunk_bboxes",
            attributes={
                _ATTR_CHUNK_COUNT: len(chunks),
                "vector_sync.pdf_size": len(content_bytes),
            },
        ):
            chunk_data: list[tuple[int, int, int, int | None, str]] = [
                (i, chunk.start_offset, chunk.end_offset, chunk.page_number, chunk.text)
                for i, chunk in enumerate(chunks)
                if chunk.page_number is not None
            ]

            page_boundaries = file_metadata.get("page_boundaries")
            if not page_boundaries:
                logger.warning(
                    "No page boundaries available, skipping bbox computation"
                )
                return

            page_boundaries_list = cast(list[dict[str, Any]], page_boundaries)

            logger.info("Computing chunk bboxes for %s PDF chunks", len(chunk_data))

            batch_results = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                lambda: PDFHighlighter.compute_chunk_bboxes_batch(
                    pdf_bytes=content_bytes,
                    chunks=chunk_data,
                    page_boundaries=page_boundaries_list,
                    full_text=content,
                )
            )

            for chunk_index, (bboxes, _) in batch_results.items():
                chunk_bboxes[chunk_index] = bboxes

            logger.info(
                "Computed bboxes for %s/%s chunks", len(chunk_bboxes), len(chunks)
            )

    # Run all embedding/highlighting operations in parallel
    # - Dense embeddings: I/O bound (API call)
    # - Sparse embeddings: CPU bound (local BM25)
    # - Highlighting: CPU bound (PyMuPDF rendering, runs in thread pool)
    with trace_operation(
        "vector_sync.parallel_processing",
        attributes={
            "vector_sync.is_pdf": is_pdf,
            _ATTR_CHUNK_COUNT: len(chunks),
        },
    ):
        async with anyio.create_task_group() as tg:
            tg.start_soon(generate_dense_embeddings)
            tg.start_soon(generate_sparse_embeddings)
            tg.start_soon(generate_highlights)

    # Prepare Qdrant points
    indexed_at = int(time.time())
    points = []

    # Decomposition payload keys (design §10.2) — written even in local mode so
    # a future migration to the external processor is friction-free. Computed
    # once per document (not per chunk). The local processor has no triage, so
    # PIPELINE_TIER is "fast"; ACL hash records at least the owner principal
    # (full share enumeration is a follow-up — a missing/partial acl_hash is
    # safe because the query-side pre-filter only applies when present + enabled).
    _embedding_identity = settings.get_embedding_model_name()
    _acl_hash = compute_acl_hash([("user", doc_task.user_id)])

    # Observed-access ACL principals (computed once per document, not per chunk).
    # Seed with the indexer (and owner, if distinct). For files — the only type
    # with cross-user dedup and globally-unique IDs (Nextcloud fileid) — union in
    # any principals already recorded so re-indexing after a content change
    # preserves visibility for readers who had previously claimed the file. For
    # note/news_item/deck_card, IDs are per-user (not globally unique) and point
    # IDs are user-agnostic, so merging another user's principals on an ID
    # collision would wrongly cross-surface their content; those types are
    # seeded with the indexer only.
    _prior_principals = (
        await existing_principals(doc_task.doc_id, doc_task.doc_type)
        if doc_task.doc_type == "file"
        else []
    )
    _acl_principals = sorted(
        set(_prior_principals)
        | {
            f"user:{doc_task.user_id}",
            f"user:{doc_task.owner_id or doc_task.user_id}",
        }
    )

    # Surface deck card data quality issues at indexing time rather than
    # only at verification time (where _verify_deck_cards falls through to
    # legacy-data pass-through when board_id/stack_id are missing). This is
    # logged once per document — not per chunk — to avoid log spam.
    if doc_task.doc_type == "deck_card":
        missing_deck_fields = [
            field for field in ("board_id", "stack_id") if not file_metadata.get(field)
        ]
        if missing_deck_fields:
            logger.warning(
                "Indexing deck_card %s for user %s with missing metadata: %s; "
                "verification will fall back to legacy-data pass-through",
                doc_task.doc_id,
                doc_task.user_id,
                missing_deck_fields,
            )

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
                    # owner_id is the UID of the file's owner — what
                    # search-time ACL expansion filters on. Today the scanner
                    # always runs as the file's owner (per-user crawl, only
                    # surfaces files the user owns or that fall under their
                    # WebDAV root), so owner_id == user_id is correct for
                    # every doc type indexed here. The fields are kept
                    # separate so a future indexer change that lets a user
                    # crawl shared-with-them content can set owner_id to the
                    # true owner without losing the "who indexed this" trail.
                    "owner_id": doc_task.owner_id or doc_task.user_id,
                    # Observed-access ACL set: every user whose scanner has seen
                    # (hence can read) this document. Seeded with the indexer (and
                    # owner, if distinct); grown lazily as other readers' scanners
                    # hit the tenant-wide dedup path. Search ORs a
                    # MatchAny(acl_principals, ["user:<me>"]) branch so a
                    # deduplicated shared file stays findable by every reader.
                    "acl_principals": _acl_principals,
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
                    # Decomposition payload keys (design §10.2), additive.
                    payload_keys.PROCESSOR_VERSION: "monolith-v1",
                    payload_keys.PARSED_AT: indexed_at,
                    # Actual tier that produced this doc (registry stamps it on
                    # the result metadata); non-PDF doc types stay "fast".
                    payload_keys.PIPELINE_TIER: file_metadata.get(
                        "pipeline_tier", "fast"
                    ),
                    payload_keys.EMBEDDING_IDENTITY: _embedding_identity,
                    payload_keys.ACL_HASH: _acl_hash,
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
                    # Chunk bbox (PDF only) — normalized rectangles in [0,1]
                    # relative to page width/height. Replaces the legacy
                    # `highlighted_page_image` (Deck #76). The page number
                    # comes from `page_number` (set above for PDF chunks).
                    **({"chunk_bbox": chunk_bboxes[i]} if i in chunk_bboxes else {}),
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
            "Failed to delete placeholder for %s_%s: %s",
            doc_task.doc_type,
            doc_task.doc_id,
            e,
        )

    # Upsert to Qdrant in batches. Now that we no longer embed PNG payloads,
    # per-point payloads are small (chunk text + small metadata), so we can
    # safely use a larger batch size.
    BATCH_SIZE = 100
    with trace_operation(
        "vector_sync.qdrant_upsert",
        attributes={
            "vector_sync.point_count": len(points),
            "vector_sync.collection": settings.get_collection_name(),
            "vector_sync.bboxes_count": len(chunk_bboxes),
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
                    "Upserted batch %s/%s",
                    batch_start // BATCH_SIZE + 1,
                    (len(points) + BATCH_SIZE - 1) // BATCH_SIZE,
                )

    logger.info(
        "Indexed %s_%s for %s (%s chunks)",
        doc_task.doc_type,
        doc_task.doc_id,
        doc_task.user_id,
        len(chunks),
        extra={
            "doc_id": doc_task.doc_id,
            "doc_type": doc_task.doc_type,
            "chunks": len(chunks),
            "status": "success",
        },
    )
