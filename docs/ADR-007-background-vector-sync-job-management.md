# ADR-007: Background Vector Database Synchronization

**Status**: Proposed
**Date**: 2025-01-08
**Supersedes**: ADR-003
**Depends On**: ADR-004 (Federated Authentication), ADR-006 (Progressive Consent)

## Context

ADR-003 proposed a vector database architecture for semantic search over Nextcloud content, introducing Qdrant as the vector store, configurable embedding strategies, and hybrid search combining semantic and keyword matching. While these technical decisions remain sound, ADR-003 was never implemented because it lacked a critical component: a practical system for keeping the vector database synchronized with changing Nextcloud content.

The challenge is not simply indexing content once, but maintaining an up-to-date vector database as users create, modify, and delete notes, files, and other documents. This synchronization must happen in the background, outside of active MCP sessions, and must operate efficiently across multiple users without manual intervention. Users should not need to understand the mechanics of vector indexing—they simply enable semantic search and the system handles the rest.

ADR-003's conceptual description of a "background sync worker" left several fundamental questions unanswered:

**Change Detection**: How does the system know when content has changed? Polling every document on every sync would be wasteful. Webhooks would be ideal but require complex Nextcloud configuration. A practical middle ground is needed.

**Work Distribution**: When multiple users enable semantic search, how should indexing work be scheduled? A naive approach might process users sequentially, causing long delays. A fair approach must balance progress across all enabled users while respecting API rate limits.

**User Experience**: What does a user see when they enable semantic search? How do they know indexing is complete? What happens if they disable it and re-enable later? The system must provide clear feedback without overwhelming users with implementation details.

**Error Handling**: What happens when the embedding API is rate-limited or temporarily unavailable? When Nextcloud returns errors? When documents are too large to process? The system must gracefully handle failures without blocking progress on unrelated documents.

**Authentication**: Background workers operate outside MCP sessions and need long-lived access to Nextcloud. ADR-003 referenced the now-deprecated ADR-002 for authentication. With ADR-004's progressive consent architecture, the authentication pattern must be clarified.

**Process Architecture**: Should background synchronization run as separate worker processes (Celery, Dramatiq) or within the MCP server process itself? The embedding workload is I/O-bound (external API calls), not CPU-bound, suggesting in-process concurrency may be sufficient.

This ADR addresses these gaps by defining a complete background synchronization architecture using in-process async concurrency. The design philosophy is event-driven and document-centric: users enable semantic search, the system automatically detects changed documents, queues them for processing, and concurrent processor tasks handle tokenization, embedding generation, and vector storage—all within the MCP server process using anyio's TaskGroup primitives.

## Decision

We will implement background vector database synchronization using anyio TaskGroups running within the MCP server process. The architecture consists of three concurrent components: a periodic scanner task that detects changed documents, an in-memory queue containing documents awaiting processing, and a pool of processor tasks that transform documents into vector embeddings and store them in Qdrant.

### Architectural Overview

The architecture treats semantic search as an automatic, continuously updating feature rather than a set of user-initiated jobs. When a user enables semantic search, they are not submitting work—they are activating a background process that will maintain their vector database without further interaction. The system's responsibility is to keep this database current with minimal latency and resource usage.

Three components run concurrently within the MCP server process:

The **scanner** is an anyio task that runs in an infinite loop with hourly sleep intervals. For each user with semantic search enabled, it fetches their content from Nextcloud and compares modification timestamps against the last indexed timestamp stored in Qdrant's vector metadata. Any document that has been created or modified since its last indexing is enqueued for processing. The scanner's job is purely discovery—it identifies work to be done but does not perform the work itself. The scanner sleeps for 3600 seconds between runs, yielding to other async tasks.

The **queue** is an in-memory `asyncio.Queue` containing individual documents awaiting processing. Each queue entry represents a single document operation: index a note, delete a file, update a calendar event. The queue has a configurable maximum size (default 10,000 documents) and provides backpressure—if the queue fills, the scanner blocks until space becomes available. This prevents memory exhaustion if processors fall behind. The queue is not persistent; pending documents are lost if the server restarts, but the next scanner run will re-discover and re-queue them.

The **processor pool** consists of multiple anyio tasks (default 3) that concurrently pull documents from the queue and transform them into vector embeddings. Each processor task runs in an infinite loop: dequeue a document, fetch its content from Nextcloud, tokenize and chunk the text, generate embeddings via the configured embedding service, and upload the resulting vectors to Qdrant. Processors run concurrently, allowing multiple documents to be processed simultaneously. The embedding workload is I/O-bound—waiting for OpenAI API responses or self-hosted embedding services—making async concurrency ideal. Processors use exponential backoff retry logic to handle temporary failures.

All three components are managed by a single anyio TaskGroup initialized during the MCP server's lifespan startup. The TaskGroup ensures coordinated lifecycle: when the server starts, all background tasks start; when the server shuts down, all tasks are gracefully cancelled. This architecture eliminates the complexity of distributed task queues while providing sufficient concurrency for I/O-bound embedding workloads.

### In-Process Concurrency Model

Running background tasks within the MCP server process rather than as separate worker processes provides significant simplicity benefits for embedding workloads. The key insight is that embedding generation is I/O-bound, not CPU-bound:

When using OpenAI's embedding API, the processor task makes an HTTP POST request and awaits the response. During this wait (typically 50-200ms), the async runtime can switch to other tasks—processing other documents, handling MCP tool calls, running the scanner. Multiple embedding requests can be in-flight simultaneously without blocking each other. The same pattern applies to self-hosted embedding services (Infinity, TEI, Ollama) accessed via HTTP.

Even local embedding models using sentence-transformers can be integrated by running the CPU-intensive embedding computation in a thread pool via `asyncio.to_thread()` or `anyio.to_thread.run_sync()`. This allows the embedding computation to happen on a background thread while the async runtime continues handling other tasks. For moderate workloads (tens to hundreds of documents per hour), this approach provides sufficient throughput without the overhead of separate processes.

The in-process model also simplifies state access. Background tasks and MCP tools run in the same process, sharing the same in-memory context, Qdrant client instances, and embedding service connections. There is no need for inter-process communication, shared volumes for token databases, or complex coordination. The scanner task can directly access the token storage that MCP tools use for Flow 2 refresh tokens. Processor tasks can use the same Qdrant client pool that search tools use.

This architecture is not suitable for CPU-bound workloads (video transcoding, image processing, ML training) where separate worker processes or machines would be necessary. But for embedding-based semantic search, where the bottleneck is I/O latency to external APIs, in-process async concurrency provides an excellent balance of simplicity and performance.

### Change Detection: ETag and Modification Timestamps

Rather than polling every document's content on every sync or attempting to configure complex webhooks, we use a timestamp comparison approach. Each vector stored in Qdrant includes an `indexed_at` field in its metadata payload, recording when the document was last processed. When the scanner runs, it fetches the list of documents from Nextcloud (which includes each document's `modified_at` timestamp and `etag`) and compares these values against the stored `indexed_at` timestamps from Qdrant.

If a document's `modified_at` is newer than its `indexed_at`, or if the document doesn't exist in Qdrant at all, it is queued for indexing. If a document exists in Qdrant but not in Nextcloud, it is queued for deletion. This approach provides efficient incremental synchronization—only changed documents are processed—without requiring Nextcloud server modifications.

The scanner's periodic execution (hourly by default) means there is some lag between a user modifying a note and that change appearing in the vector database. For semantic search use cases, this lag is acceptable. Users are searching for knowledge and context, not expecting instant reflection of edits. The system optimizes for correctness and resource efficiency over real-time synchronization.

### Queue Model: In-Memory Document Queue

The task queue is implemented using Python's built-in `asyncio.Queue`, which provides async-safe enqueue and dequeue operations. Each queue entry has a simple structure:

```python
@dataclass
class DocumentTask:
    user_id: str
    doc_id: str
    doc_type: str  # "note", "file", "calendar"
    operation: str  # "index" or "delete"
    modified_at: int
```

This granular approach allows the system to make incremental progress even when processing is slow. If a user has 1,000 notes and 10 have changed, only 10 queue entries are created. If processors encounter errors on specific documents, those documents fail independently—successful processing of other documents represents real forward progress.

The queue is configured with a maximum size (default 10,000) to prevent unbounded memory growth. If the scanner attempts to enqueue when the queue is full, the `put()` operation blocks until space becomes available. This provides natural backpressure—the scanner waits for processors to catch up rather than overwhelming system memory.

The in-memory queue is ephemeral: pending documents are lost if the server restarts. This is an acceptable trade-off because the scanner will re-discover unindexed documents on its next run (hourly). The system achieves eventual consistency—all changed documents will eventually be indexed—without the complexity of persistent queue storage. For deployments requiring guaranteed processing of every document, a persistent queue backed by SQLite could be added, but this is not necessary for semantic search workloads.

### Processor Pool: Concurrent Document Processing

The processor pool consists of multiple anyio tasks running concurrently within the same process. Each processor task follows the same pattern: pull a document from the queue, process it, mark the queue task complete, repeat. Multiple processors can work simultaneously because the embedding workload is I/O-bound:

```python
async def processor_task(worker_id: int, ctx: LifespanContext):
    """Process documents from queue."""
    logger.info(f"Processor {worker_id} started")

    while not ctx.shutdown_event.is_set():
        try:
            # Get document with timeout (allows checking shutdown periodically)
            doc_task = await asyncio.wait_for(
                ctx.document_queue.get(),
                timeout=1.0
            )

            # Process document (I/O bound - embedding API calls)
            await process_document(doc_task, ctx)

            # Mark complete
            ctx.document_queue.task_done()

        except asyncio.TimeoutError:
            # No documents available, check shutdown and continue
            continue
        except Exception as e:
            logger.error(f"Processor {worker_id} error: {e}")
            ctx.document_queue.task_done()
```

Each processor is an independent task that can make progress without blocking others. When one processor is waiting for an embedding API response, other processors continue working on different documents. This natural parallelism emerges from anyio's async runtime without the complexity of multiprocessing.

The number of concurrent processors is configurable (default 3). More processors increase throughput for I/O-bound workloads, up to the point where embedding API rate limits become the bottleneck. For OpenAI's embedding API (rate limit: 3,000 requests/minute), 3-5 concurrent processors provide good throughput without hitting limits. For self-hosted embedding services with higher capacity, more processors can be beneficial.

Processor tasks implement retry logic with exponential backoff for transient failures. If an embedding API request times out or returns a 429 rate limit error, the processor sleeps for an increasing duration (1s, 2s, 4s) before retrying. After three retries, the document is logged as failed and dropped—the queue continues processing other documents. The next scanner run will re-discover the failed document and try again, ensuring eventual consistency.

### State Tracking: Qdrant as Source of Truth

The system uses Qdrant's vector metadata as the single source of truth for indexing state. When the scanner needs to determine which documents have changed, it queries Qdrant for existing vectors belonging to the user and extracts the `indexed_at` timestamps from their metadata payloads.

This eliminates the synchronization problem between an external state table and the actual vector database. If a vector exists in Qdrant with an `indexed_at` timestamp, the document has been indexed at that time—there is no possibility of drift between a state table saying "document indexed" and the actual absence of vectors. If vectors are deleted (either manually or when a user disables semantic search), the state is automatically correct because the vectors themselves are gone.

Querying Qdrant for state does introduce a performance consideration—each scanner run must retrieve metadata for all of a user's vectors to compare timestamps. However, Qdrant's scroll API is optimized for this use case, and the system can retrieve thousands of metadata entries efficiently. The scanner only requests the minimal payload fields needed for comparison (`doc_id`, `indexed_at`, `etag`), avoiding the overhead of retrieving full vector data or embeddings.

### User Settings and Controls

User interaction with the vector synchronization system is intentionally minimal. A simple SQLite table stores user preferences:

```sql
CREATE TABLE vector_sync_settings (
    user_id TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    last_scan_at INTEGER,
    last_sync_status TEXT,  -- "idle" or "syncing"
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

When a user enables semantic search, the system performs three actions: it verifies the user has completed Flow 2 provisioning (obtaining the necessary offline access tokens), updates the settings table to mark the user as enabled, and triggers an immediate scanner run to queue all of the user's existing documents for initial indexing. From that point forward, the periodic scanner includes this user in its hourly scans.

When a user disables semantic search, the system updates the settings table to mark the user as disabled and deletes all of the user's vectors from Qdrant. This clean-slate approach ensures that disabled users consume no vector storage and reduces search index size. If the user later re-enables semantic search, the system performs a fresh initial indexing.

The status API provides users with visibility into the synchronization state without exposing the underlying queue mechanics. When a user queries their sync status, the system returns the count of indexed documents (queried from Qdrant), the count of pending documents in the queue (via `queue.qsize()`), and a simple status flag indicating whether synchronization is actively occurring. The display reads something like: "1,234 documents indexed, Status: Syncing (45 pending)" or "1,234 documents indexed, Status: Idle".

There are no manual sync triggers, no job cancellation controls, and no per-document status tracking exposed to users. The system operates automatically, and users see only the high-level outcome: how many documents are indexed and whether work is in progress.

### MCP Tool Interface

The MCP tool interface reflects the simplicity of the user model:

```python
@mcp.tool()
@require_scopes("sync:write")
async def enable_vector_sync(ctx: Context) -> dict:
    """
    Enable automatic background vector synchronization for semantic search.

    Once enabled, the system will automatically maintain a vector database
    of your Nextcloud content, enabling semantic search capabilities. No
    further action is required - synchronization happens in the background.

    Returns:
        Status message and current indexed document count
    """
    user_id = get_user_id_from_context(ctx)

    # Verify offline access provisioning
    token_storage = get_token_storage(ctx)
    refresh_token = await token_storage.get_refresh_token(user_id)
    if not refresh_token:
        return {
            "status": "error",
            "message": "You must provision offline access first. "
                       "Run the 'provision_nextcloud_access' tool."
        }

    # Enable in settings
    settings_repo = VectorSyncSettingsRepository()
    await settings_repo.upsert(user_id=user_id, enabled=True)

    # Trigger immediate scan by waking up scanner
    # (scanner will detect new enabled user on next iteration)
    lifespan_ctx = ctx.request_context.lifespan_context
    if hasattr(lifespan_ctx, 'scanner_wake_event'):
        lifespan_ctx.scanner_wake_event.set()

    return {
        "status": "enabled",
        "message": "Vector sync enabled. Initial indexing will begin shortly.",
        "note": "You can check progress with get_vector_sync_status()"
    }


@mcp.tool()
@require_scopes("sync:write")
async def disable_vector_sync(ctx: Context) -> dict:
    """
    Disable vector synchronization and remove all indexed vectors.

    This will stop automatic indexing and delete all vector database
    content for your account. Semantic search will no longer work until
    you re-enable synchronization.

    Returns:
        Confirmation message
    """
    user_id = get_user_id_from_context(ctx)

    # Disable in settings
    settings_repo = VectorSyncSettingsRepository()
    await settings_repo.update(user_id=user_id, enabled=False)

    # Delete all vectors from Qdrant
    qdrant_client = get_qdrant_client()
    await qdrant_client.delete(
        collection_name="nextcloud_content",
        points_selector=Filter(
            must=[
                FieldCondition(
                    key="user_id",
                    match=MatchValue(value=user_id)
                )
            ]
        )
    )

    return {
        "status": "disabled",
        "message": "Vector sync disabled. All indexed content removed."
    }


@mcp.tool()
@require_scopes("sync:read")
async def get_vector_sync_status(ctx: Context) -> dict:
    """
    Get current vector synchronization status.

    Shows how many documents have been indexed and whether background
    synchronization is currently active.

    Returns:
        Indexed count, pending count, and sync status
    """
    user_id = get_user_id_from_context(ctx)

    # Check if enabled
    settings_repo = VectorSyncSettingsRepository()
    settings = await settings_repo.get(user_id)

    if not settings or not settings.enabled:
        return {
            "enabled": False,
            "message": "Vector sync is not enabled for this user."
        }

    # Get indexed count from Qdrant
    qdrant_client = get_qdrant_client()
    count = await qdrant_client.count(
        collection_name="nextcloud_content",
        count_filter=Filter(
            must=[
                FieldCondition(
                    key="user_id",
                    match=MatchValue(value=user_id)
                )
            ]
        )
    )

    # Get pending queue size from in-memory queue
    lifespan_ctx = ctx.request_context.lifespan_context
    pending_count = 0
    if hasattr(lifespan_ctx, 'document_queue'):
        pending_count = lifespan_ctx.document_queue.qsize()

    status = "syncing" if pending_count > 0 else "idle"

    return {
        "enabled": True,
        "indexed_count": count.count,
        "pending_count": pending_count,
        "status": status,
        "message": f"{count.count} documents indexed, Status: {status.title()}"
                  + (f" ({pending_count} pending)" if pending_count > 0 else "")
    }
```

The web UI (`/user/page` route) mirrors these controls with a simple toggle switch for enabling/disabling sync and a status display showing indexed counts and sync state. There is no job history, no detailed progress bars, no per-document status—just the essential information users need.

### Authentication and Offline Access

Background synchronization depends critically on ADR-004's Flow 2 refresh tokens. When a user enables semantic search, the system first verifies they have completed the provisioning flow via the `provision_nextcloud_access` tool. This flow grants the MCP server a refresh token with `offline_access` scope and audience set to `nextcloud`.

The scanner and processor tasks use these refresh tokens to obtain short-lived access tokens for making Nextcloud API calls on behalf of users. This happens entirely in the background, outside any active MCP session. The tokens are never exposed to MCP clients and are stored encrypted in the `idp_tokens` SQLite table. Because background tasks run in the same process as MCP tools, they share access to the token storage—no volume sharing or inter-process communication is needed.

If a user's refresh token expires or is revoked, background processing for that user will fail. The processor's error handling logs these authentication failures and marks the user's sync status as errored. The next time the user interacts via MCP tools, they will see a message indicating they need to re-provision offline access.

This authentication model respects the security boundaries established in ADR-004: MCP session tokens (Flow 1) are never used for background operations, only explicitly provisioned offline tokens (Flow 2) are used, and token management is transparent to users who simply see "sync enabled" or "access required".

## Implementation

### Lifespan Management

Background tasks are initialized and managed using FastMCP's lifespan context and anyio TaskGroups:

```python
from contextlib import asynccontextmanager
import asyncio
import anyio
from fastmcp import FastMCP

mcp = FastMCP("Nextcloud")

@asynccontextmanager
async def lifespan(app):
    """
    Initialize background sync tasks on server startup.

    Creates an anyio TaskGroup that manages:
    - Scanner task (periodic document discovery)
    - Processor pool (concurrent document indexing)

    All tasks are gracefully cancelled on shutdown.
    """

    # Initialize shared state
    document_queue = asyncio.Queue(maxsize=10000)
    shutdown_event = anyio.Event()
    scanner_wake_event = anyio.Event()

    # Store in app state for access from tools
    app.state.document_queue = document_queue
    app.state.shutdown_event = shutdown_event
    app.state.scanner_wake_event = scanner_wake_event

    async with anyio.create_task_group() as tg:
        # Start scanner task
        tg.start_soon(
            scanner_task,
            document_queue,
            shutdown_event,
            scanner_wake_event
        )

        # Start processor pool (3 concurrent workers)
        for i in range(settings.vector_sync_processor_workers):
            tg.start_soon(
                processor_task,
                i,
                document_queue,
                shutdown_event
            )

        logger.info("Background sync tasks started")

        # Yield to run server
        yield

        # Shutdown signal
        shutdown_event.set()

        # TaskGroup automatically cancels all tasks on exit
        logger.info("Background sync tasks stopped")

# Register lifespan
mcp.app.router.lifespan_context = lifespan
```

### Scanner Task Implementation

The scanner runs in an infinite loop with periodic sleep intervals:

```python
async def scanner_task(
    document_queue: asyncio.Queue,
    shutdown_event: anyio.Event,
    wake_event: anyio.Event
):
    """
    Periodic scanner that detects changed documents for all enabled users.

    Runs every hour (configurable), or immediately when wake_event is set.
    For each enabled user:
    1. Fetch all documents from Nextcloud
    2. Query Qdrant for existing indexed state
    3. Compare timestamps to identify changes
    4. Queue changed documents for processing
    """
    logger.info("Scanner task started")

    while not shutdown_event.is_set():
        try:
            # Scan all enabled users
            await scan_all_enabled_users(document_queue)

        except Exception as e:
            logger.error(f"Scanner error: {e}", exc_info=True)

        # Sleep until next interval or wake event
        try:
            with anyio.move_on_after(settings.vector_sync_scan_interval):
                # Wait for wake event or shutdown
                async with anyio.create_task_group() as tg:
                    async def wait_wake():
                        await wake_event.wait()
                        wake_event.clear()

                    async def wait_shutdown():
                        await shutdown_event.wait()

                    tg.start_soon(wait_wake)
                    tg.start_soon(wait_shutdown)

                    # First event wins
                    tg.cancel_scope.cancel()

        except anyio.get_cancelled_exc_class():
            # Shutdown or wake, continue loop
            pass

    logger.info("Scanner task stopped")


async def scan_all_enabled_users(document_queue: asyncio.Queue):
    """Scan all enabled users and queue changed documents."""
    settings_repo = VectorSyncSettingsRepository()
    enabled_users = await settings_repo.get_enabled_users()

    logger.info(f"Scanning {len(enabled_users)} enabled users")

    for user in enabled_users:
        try:
            await scan_user_documents(user.user_id, document_queue)
        except Exception as e:
            logger.error(f"Failed to scan user {user.user_id}: {e}")
            await settings_repo.update(
                user_id=user.user_id,
                last_sync_status="error"
            )


async def scan_user_documents(
    user_id: str,
    document_queue: asyncio.Queue,
    initial_sync: bool = False
):
    """
    Scan a single user's documents and queue changes.

    Args:
        user_id: User to scan
        document_queue: Queue to enqueue changed documents
        initial_sync: If True, queue all documents (first-time sync)
    """
    # Get Nextcloud client using Flow 2 refresh token
    token_storage = get_token_storage()
    refresh_token = await token_storage.get_refresh_token(user_id)
    if not refresh_token:
        raise NotProvisionedError(f"User {user_id} not provisioned")

    idp_client = get_idp_client()
    access_token_response = await idp_client.refresh_token(
        refresh_token=refresh_token.token,
        audience='nextcloud'
    )

    client = NextcloudClient.from_token(
        base_url=settings.nextcloud_host,
        token=access_token_response.access_token,
        username=user_id
    )

    # Fetch all notes
    notes = await client.notes.list_notes()

    if initial_sync:
        # Queue everything on first sync
        for note in notes:
            await document_queue.put(
                DocumentTask(
                    user_id=user_id,
                    doc_id=str(note.id),
                    doc_type="note",
                    operation="index",
                    modified_at=note.modified
                )
            )
        logger.info(f"Queued {len(notes)} documents for initial sync: {user_id}")
        return

    # Get indexed state from Qdrant
    qdrant_client = get_qdrant_client()
    scroll_result = await qdrant_client.scroll(
        collection_name="nextcloud_content",
        scroll_filter=Filter(
            must=[
                FieldCondition(key="user_id", match=MatchValue(value=user_id)),
                FieldCondition(key="doc_type", match=MatchValue(value="note"))
            ]
        ),
        with_payload=["doc_id", "indexed_at"],
        with_vectors=False,
        limit=10000
    )

    indexed_docs = {
        point.payload["doc_id"]: point.payload["indexed_at"]
        for point, _ in scroll_result[0]
    }

    # Compare and queue changes
    queued = 0
    for note in notes:
        doc_id = str(note.id)
        indexed_at = indexed_docs.get(doc_id)

        # Queue if never indexed or modified since last index
        if indexed_at is None or note.modified > indexed_at:
            await document_queue.put(
                DocumentTask(
                    user_id=user_id,
                    doc_id=doc_id,
                    doc_type="note",
                    operation="index",
                    modified_at=note.modified
                )
            )
            queued += 1

    # Check for deleted documents (in Qdrant but not in Nextcloud)
    nextcloud_doc_ids = {str(note.id) for note in notes}
    for doc_id in indexed_docs:
        if doc_id not in nextcloud_doc_ids:
            await document_queue.put(
                DocumentTask(
                    user_id=user_id,
                    doc_id=doc_id,
                    doc_type="note",
                    operation="delete",
                    modified_at=0
                )
            )
            queued += 1

    logger.info(f"Queued {queued} documents for incremental sync: {user_id}")

    # Update settings
    settings_repo = VectorSyncSettingsRepository()
    await settings_repo.update(
        user_id=user_id,
        last_scan_at=int(time.time()),
        last_sync_status="idle" if queued == 0 else "syncing"
    )
```

### Processor Task Implementation

Multiple processor tasks run concurrently, each pulling from the shared queue:

```python
async def processor_task(
    worker_id: int,
    document_queue: asyncio.Queue,
    shutdown_event: anyio.Event
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
    """
    logger.info(f"Processor {worker_id} started")

    while not shutdown_event.is_set():
        try:
            # Get document with timeout (allows checking shutdown)
            doc_task = await asyncio.wait_for(
                document_queue.get(),
                timeout=1.0
            )

            # Process document
            await process_document(doc_task)

            # Mark complete
            document_queue.task_done()

        except asyncio.TimeoutError:
            # No documents available, continue
            continue

        except Exception as e:
            logger.error(
                f"Processor {worker_id} error processing "
                f"{doc_task.doc_type}_{doc_task.doc_id}: {e}",
                exc_info=True
            )
            # Mark task done even on error to prevent queue blocking
            try:
                document_queue.task_done()
            except ValueError:
                pass

    logger.info(f"Processor {worker_id} stopped")


async def process_document(doc_task: DocumentTask):
    """
    Process a single document: fetch, tokenize, embed, store in Qdrant.

    Implements retry logic with exponential backoff for transient failures.
    """
    logger.debug(
        f"Processing {doc_task.doc_type}_{doc_task.doc_id} "
        f"for {doc_task.user_id} ({doc_task.operation})"
    )

    qdrant_client = get_qdrant_client()

    # Handle deletion
    if doc_task.operation == "delete":
        await qdrant_client.delete(
            collection_name="nextcloud_content",
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="user_id",
                        match=MatchValue(value=doc_task.user_id)
                    ),
                    FieldCondition(
                        key="doc_id",
                        match=MatchValue(value=doc_task.doc_id)
                    ),
                    FieldCondition(
                        key="doc_type",
                        match=MatchValue(value=doc_task.doc_type)
                    )
                ]
            )
        )
        logger.info(
            f"Deleted {doc_task.doc_type}_{doc_task.doc_id} "
            f"for {doc_task.user_id}"
        )
        return

    # Handle indexing with retry
    max_retries = 3
    retry_delay = 1.0

    for attempt in range(max_retries):
        try:
            await _index_document(doc_task, qdrant_client)
            return  # Success

        except (EmbeddingAPIError, QdrantTimeout, HTTPStatusError) as e:
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


async def _index_document(doc_task: DocumentTask, qdrant_client):
    """Index a single document (called by process_document with retry)."""

    # Get Nextcloud client using Flow 2 refresh token
    token_storage = get_token_storage()
    refresh_token = await token_storage.get_refresh_token(doc_task.user_id)
    if not refresh_token:
        raise NotProvisionedError(f"User {doc_task.user_id} not provisioned")

    idp_client = get_idp_client()
    access_token_response = await idp_client.refresh_token(
        refresh_token=refresh_token.token,
        audience='nextcloud'
    )

    client = NextcloudClient.from_token(
        base_url=settings.nextcloud_host,
        token=access_token_response.access_token,
        username=doc_task.user_id
    )

    # Fetch document content
    if doc_task.doc_type == "note":
        document = await client.notes.get_note(int(doc_task.doc_id))
        content = f"{document['title']}\n\n{document['content']}"
        title = document['title']
        etag = document.get('etag', '')
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
        points.append(
            PointStruct(
                id=f"{doc_task.doc_type}_{doc_task.doc_id}_{i}",
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
                    "total_chunks": len(chunks)
                }
            )
        )

    # Upsert to Qdrant
    await qdrant_client.upsert(
        collection_name="nextcloud_content",
        points=points,
        wait=True
    )

    logger.info(
        f"Indexed {doc_task.doc_type}_{doc_task.doc_id} for {doc_task.user_id} "
        f"({len(chunks)} chunks)"
    )
```

### Configuration

```bash
# Vector Sync Configuration
VECTOR_SYNC_ENABLED=true
VECTOR_SYNC_SCAN_INTERVAL=3600  # Scanner runs every 3600 seconds (1 hour)
VECTOR_SYNC_PROCESSOR_WORKERS=3  # Number of concurrent processor tasks
VECTOR_SYNC_QUEUE_MAX_SIZE=10000  # Maximum documents in queue

# Qdrant Configuration (from ADR-003)
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=<api-key>
QDRANT_COLLECTION=nextcloud_content

# Embedding Configuration (from ADR-003)
OPENAI_API_KEY=<api-key>
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

### Docker Compose

The simplified architecture requires only a single MCP server container:

```yaml
services:
  # MCP Server with integrated background sync
  mcp:
    build: .
    command: ["--transport", "sse"]
    ports:
      - "8000:8000"
    depends_on:
      - app
      - qdrant
    environment:
      # Nextcloud connection
      - NEXTCLOUD_HOST=http://app:80

      # OAuth configuration
      - ENABLE_OFFLINE_ACCESS=true
      - TOKEN_ENCRYPTION_KEY=${TOKEN_ENCRYPTION_KEY}
      - IDP_DISCOVERY_URL=${IDP_DISCOVERY_URL}

      # Qdrant connection
      - QDRANT_URL=http://qdrant:6333
      - QDRANT_API_KEY=${QDRANT_API_KEY}

      # Embedding service
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - OPENAI_EMBEDDING_MODEL=text-embedding-3-small

      # Vector sync configuration
      - VECTOR_SYNC_ENABLED=true
      - VECTOR_SYNC_SCAN_INTERVAL=3600
      - VECTOR_SYNC_PROCESSOR_WORKERS=3

      # Data directory
      - DATA_DIR=/app/data
    volumes:
      - mcp-data:/app/data

  # Qdrant vector database
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
    volumes:
      - qdrant-data:/qdrant/storage
    environment:
      - QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY}

volumes:
  mcp-data:
  qdrant-data:
```

## Consequences

### Benefits

This architecture achieves automatic, maintenance-free vector database synchronization with significantly reduced operational complexity. Users enable semantic search once and the system handles everything else—detecting changes, queuing work, processing documents, and updating the vector database. The user experience is simple: flip a switch, see a status count, and semantic search just works.

The in-process design eliminates entire categories of deployment complexity. There is no need for separate worker containers, no distributed task queue broker, no inter-process communication, no shared volumes for state synchronization. The MCP server is a single container with all functionality included. This simplifies deployment, reduces resource usage (one process instead of three), and makes debugging significantly easier—all logs are in one place, and a single debugger session can trace execution from MCP tool calls through background processing.

The document-centric queue model provides robustness and incremental progress. A single problematic document cannot block processing of other documents. Temporary failures retry automatically with exponential backoff, and permanent failures (oversized documents, corrupted content) are logged but don't halt the entire system. The queue naturally handles bursts of activity—if many users enable semantic search simultaneously, documents are processed in order without overwhelming downstream systems.

Using Qdrant metadata as the source of truth for indexing state eliminates an entire class of synchronization bugs. There is no possibility of a state table claiming a document is indexed when vectors are missing, or vice versa. The indexed state and the actual vectors are atomically coupled—if vectors exist with an `indexed_at` timestamp, the document was indexed at that time.

The async concurrency model provides excellent throughput for I/O-bound embedding workloads. Multiple processor tasks can have embedding API requests in-flight simultaneously, maximizing utilization of external services without the overhead of multiprocessing. For OpenAI's embedding API with typical 100ms latency, three concurrent processors can maintain approximately 30 embeddings per second, sufficient for incremental sync workloads where most documents haven't changed.

### Limitations

The in-memory queue means pending documents are lost if the server restarts. This is mitigated by the scanner's hourly execution—any documents that were queued but not processed will be re-discovered and re-queued on the next scan. For semantic search workloads, this eventual consistency is acceptable. For applications requiring guaranteed processing of every document without possible loss, a persistent queue backed by SQLite could be added, trading simplicity for durability.

The in-process architecture limits horizontal scaling. All background processing happens within a single server instance, so adding more MCP servers does not increase background processing capacity. Each server would run its own scanner and processors, potentially causing duplicate work. For very large deployments (thousands of users, millions of documents), a distributed task queue architecture (Celery with Redis, SQS workers) would be more appropriate. However, for moderate deployments (hundreds of users, hundreds of thousands of documents), the simplicity-performance trade-off strongly favors in-process execution.

The scanner's hourly interval introduces lag between content changes and vector database updates. If a user creates a note at 9:05 AM and the scanner last ran at 9:00 AM, that note won't be indexed until the 10:00 AM scan. For semantic search use cases this lag is typically acceptable—users are searching for knowledge, not expecting instant reflection of edits. Applications requiring near-real-time indexing would need a different approach, such as webhook-triggered incremental updates.

The number of concurrent processor tasks is limited by the async runtime's capacity. While anyio can handle hundreds of concurrent tasks, practical limits emerge around 5-10 processor tasks for embedding workloads. Beyond this point, embedding API rate limits become the bottleneck rather than concurrency limits. For OpenAI's 3,000 requests/minute limit, even a single processor can keep the API saturated during burst periods.

The authentication dependency on Flow 2 refresh tokens means users must complete the provisioning flow before enabling semantic search. If a user's refresh token expires or is revoked, background synchronization silently fails until they re-provision. While error handling logs these failures and updates sync status, the user experience could be improved with proactive notification when re-provisioning is needed.

### Performance Characteristics

With three concurrent processor tasks and OpenAI's embedding API (100ms average latency), the system can process approximately 30 documents per second under ideal conditions. This translates to 1,800 documents per minute or 108,000 documents per hour. For a deployment with 100 users averaging 1,000 notes each, full initial indexing would complete within one hour of enabling semantic search.

Incremental syncs are much faster because most documents haven't changed between scanner runs. If the typical change rate is 1% of documents per hour (10 notes per user), the system processes 1,000 documents per scan cycle with the same 100 users, completing within 30 seconds. This keeps the vector database current with minimal lag.

The scanner itself is lightweight, making only API calls to list documents and scroll Qdrant metadata. With efficient API design (batch fetching, minimal payloads), a single scanner invocation for 100 users completes within minutes. The hourly scan interval provides ample time for completion even with occasional slowdowns.

The in-memory queue has negligible memory overhead. Each `DocumentTask` is approximately 200 bytes, so a full queue of 10,000 documents consumes only 2MB of RAM. The primary memory consumption comes from the Qdrant client connection pool and embedding service clients, which are shared across all tasks.

### Cost Estimates

For a deployment using OpenAI embeddings with 100 users averaging 500 notes each (50,000 total documents):

Initial indexing cost: 50,000 documents × 250 words/document × $0.00002/1000 tokens ≈ $2.50

Monthly incremental sync cost (assuming 1% daily change rate): 50,000 × 0.01 × 30 days × 250 words × $0.00002/1000 tokens ≈ $1.88/month

Total first month: $4.38, subsequent months: $1.88

Infrastructure costs (self-hosted): Qdrant requires approximately 200MB RAM for 50,000 vectors (4KB per document), the MCP server with background tasks uses approximately 512MB RAM (same as without background sync because tasks are I/O-bound), total infrastructure cost is dominated by Qdrant storage.

Alternative with self-hosted embeddings: Zero per-document costs, requires GPU instance ($0.50/hour = $360/month for 24/7 operation) or CPU-only processing (negligible cost, ~10x slower embedding generation, can be run via `anyio.to_thread.run_sync()` in processor tasks).

## Alternatives Considered

### Celery with Distributed Workers

A distributed task queue architecture using Celery with Redis broker and separate worker processes would provide better horizontal scaling and guaranteed task processing (persistent queue). This is the traditional approach for background job processing.

However, this architecture adds significant complexity: separate containers for workers and beat scheduler, Redis or RabbitMQ broker deployment, shared volume configuration for token database access, inter-process communication overhead, and more complex debugging (logs scattered across multiple processes). For embedding workloads that are I/O-bound rather than CPU-bound, the scaling benefits don't justify the complexity cost. The in-process anyio approach provides sufficient throughput for moderate deployments while dramatically reducing operational overhead.

The Celery approach would be appropriate for very large deployments (thousands of users, millions of documents) where horizontal scaling is essential, or for workloads that are CPU-bound (local embedding models requiring significant computation). For the common case of API-based embeddings and moderate scale, in-process execution is superior.

### Webhook-Driven Synchronization

An event-driven approach using Nextcloud webhooks to trigger indexing immediately upon document creation or modification would provide near-real-time synchronization with minimal resource waste. This would be ideal for user experience but requires significant infrastructure complexity.

Nextcloud webhook configuration varies by installation and app. Some apps support webhooks, others don't. Configuring webhooks requires server administrator access and per-app setup. The MCP server would need a public HTTP endpoint to receive webhook callbacks, adding deployment complexity and security considerations.

For these reasons, the timestamp-based polling approach was chosen despite its higher latency. It works uniformly across all Nextcloud installations and apps without requiring server configuration. Future iterations could add webhook support as an optional enhancement while maintaining polling as the default.

### Real-Time Indexing During MCP Tool Calls

Rather than background synchronization, the system could index documents inline when they are created or modified via MCP tools. Creating a note would trigger immediate embedding generation and Qdrant storage before returning success.

This would provide instant semantic search availability but creates significant user-facing latency. Embedding generation takes 100-500ms per document, unacceptable for interactive operations. It also wouldn't handle documents created outside MCP tools (via the Nextcloud web UI, mobile apps, etc.).

Background synchronization decouples user operations from indexing latency and handles all content regardless of creation method. The hourly lag is an acceptable trade-off for responsive tool performance.

### Persistent Queue with SQLite

The in-memory `asyncio.Queue` could be replaced with a persistent queue backed by SQLite, ensuring that pending documents survive server restarts. Each queue operation would write to the database, providing durability guarantees.

This would eliminate the possibility of losing pending documents during restarts, but adds complexity and performance overhead. Every enqueue and dequeue operation would require a database write, adding latency and increasing I/O load. For semantic search workloads where the scanner runs hourly and will re-discover any lost documents, the durability benefit doesn't justify the complexity cost.

A persistent queue would be more appropriate for applications requiring guaranteed processing of every document with no possibility of loss, or for workloads with very long processing times where restarts would result in significant lost progress.

## Related Decisions

- **ADR-003**: Vector Database and Semantic Search Architecture (superseded by this ADR for background synchronization, core technical decisions retained)
- **ADR-004**: Federated Authentication Architecture for Offline Access (provides Flow 2 refresh tokens used by background tasks)
- **ADR-006**: Progressive Consent via URL Elicitation (defines provisioning UX that enables offline access)

## References

- [anyio Documentation](https://anyio.readthedocs.io/)
- [anyio TaskGroups](https://anyio.readthedocs.io/en/stable/tasks.html)
- [asyncio Queue](https://docs.python.org/3/library/asyncio-queue.html)
- [FastMCP Lifespan Events](https://github.com/jlowin/fastmcp)
- [Qdrant Scroll API](https://qdrant.tech/documentation/concepts/points/#scroll-points)
- [RFC 6749: OAuth 2.0 Authorization Framework](https://datatracker.ietf.org/doc/html/rfc6749)
