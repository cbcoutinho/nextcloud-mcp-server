# ADR-010: Webhook-Based Vector Database Synchronization

**Status**: Proposed
**Date**: 2025-01-10
**Depends On**: ADR-007 (Background Vector Sync)

## Context

ADR-007 established a background synchronization architecture for maintaining the vector database using periodic polling. The scanner task runs on a configurable interval (default 3600 seconds / 1 hour) to detect changed documents across Nextcloud apps. While this polling approach is simple and reliable, it introduces significant latency between content changes and vector database updates.

### Current Polling Architecture

The existing scanner implementation in `nextcloud_mcp_server/vector/scanner.py` operates as follows:

1. **Periodic Scanning**: The scanner task sleeps for `vector_sync_scan_interval` seconds between runs
2. **Change Detection**: For each scan, it:
   - Fetches all documents from Nextcloud (notes, calendar events, etc.)
   - Queries Qdrant for the last indexed timestamp of each document
   - Compares modification timestamps to detect changes
   - Queues changed documents for processing
3. **Document Processing**: Processor tasks pull from the queue, generate embeddings, and update Qdrant

This architecture works but has fundamental limitations:

**Latency**: With a 1-hour scan interval, content changes can take up to 1 hour to appear in semantic search results. For time-sensitive use cases (e.g., "What's on my calendar today?"), this delay is problematic.

**API Load**: Every scan fetches *all* documents for *all* enabled users, regardless of whether anything changed. For large deployments with thousands of documents, this generates significant unnecessary API traffic to Nextcloud.

**Resource Waste**: The scanner and processors consume compute resources even when no content has changed. During periods of low activity, the system performs wasteful polling.

**Scalability**: As the number of users and documents grows, the time required to complete a full scan increases. Eventually, the scan duration may exceed the scan interval, causing scans to run continuously without idle periods.

**Rate Limiting**: Fetching all documents for all users in rapid succession can trigger Nextcloud's rate limiting, especially on shared hosting environments with restrictive API quotas.

These limitations are inherent to any polling-based architecture. Reducing the scan interval (e.g., to 5 minutes) reduces latency but exacerbates API load, resource waste, and rate limiting issues. The fundamental problem is that the system has no way to know *when* content changes occur—it must repeatedly check to find out.

### Nextcloud Webhook Listeners

Nextcloud provides a webhook_listeners app (bundled with Nextcloud 30+) that enables push-based change notifications. Instead of polling for changes, external services can register webhook endpoints and receive HTTP POST requests when specific events occur. Administrators register these webhooks using Nextcloud's OCS API or occ commands.

The webhook_listeners app supports events for all Nextcloud apps relevant to this MCP server's vector database:

**Files/Notes Events** (notes are stored as files):
- `OCP\Files\Events\Node\NodeCreatedEvent`
- `OCP\Files\Events\Node\NodeWrittenEvent`
- `OCP\Files\Events\Node\NodeDeletedEvent`
- `OCP\Files\Events\Node\NodeRenamedEvent`
- `OCP\Files\Events\Node\NodeCopiedEvent`

**Calendar Events**:
- `OCP\Calendar\Events\CalendarObjectCreatedEvent`
- `OCP\Calendar\Events\CalendarObjectUpdatedEvent`
- `OCP\Calendar\Events\CalendarObjectDeletedEvent`
- `OCP\Calendar\Events\CalendarObjectMovedEvent`

**Tables Events**:
- `OCA\Tables\Event\RowAddedEvent`
- `OCA\Tables\Event\RowUpdatedEvent`
- `OCA\Tables\Event\RowDeletedEvent`

**Deck Events** (via file events since cards are stored as files in some configurations)

Each webhook notification includes rich metadata:
- User ID who triggered the event
- Timestamp of the event
- Document ID and metadata
- Operation type (create, update, delete)
- Path information (for files)

Webhook notifications are dispatched via background jobs, with configurable delivery guarantees. Administrators can set up dedicated webhook worker processes to achieve near-real-time delivery (within seconds of the triggering event).

### Why Not Replace Polling Entirely?

While webhooks provide superior latency and efficiency, they cannot fully replace polling:

**Missed Events**: If the MCP server is down when a webhook fires, the notification is lost. Nextcloud's background job system processes webhooks asynchronously, but does not queue failed deliveries indefinitely.

**Administrator Setup**: Webhooks must be registered by Nextcloud administrators using the OCS API or occ commands. This is an optional optimization that administrators can enable when they want to reduce polling frequency.

**Filter Configuration**: Webhook filters must be carefully configured to avoid notification floods. A poorly configured filter could send thousands of notifications for bulk operations (e.g., importing a calendar with hundreds of events).

**Graceful Degradation**: In environments where webhooks are not configured, the system continues using polling without any degradation in functionality.

**Deletion Detection**: Nextcloud's webhook system does not guarantee delivery of deletion events if the user's account is removed or the app is uninstalled. Periodic polling provides a safety mechanism to detect orphaned documents.

A complementary architecture where webhooks supplement (but don't replace) polling provides low-latency updates when configured, with polling ensuring reliability.

### Design Considerations

**Push vs Pull Trade-offs**:
Webhooks introduce new failure modes (network issues, endpoint unavailability, notification floods) that polling avoids. The webhook endpoint must handle failures gracefully without blocking semantic search functionality.

**Webhook Endpoint Security**:
The MCP server exposes an HTTP endpoint to receive webhooks. Authentication is optional—in production deployments, administrators can configure Nextcloud to send an `Authorization` header that the MCP server validates. For local development, authentication can be disabled for simplicity.

**Idempotency**:
The system may receive duplicate notifications (webhook + next scan) or out-of-order notifications (update fires before create completes). Document processing must be idempotent—processing the same document multiple times produces the same result.

**Asynchronous Processing**:
Nextcloud processes webhooks via background jobs, introducing delivery latency (typically seconds to minutes depending on background job configuration). This affects testing strategies—integration tests cannot rely on immediate webhook delivery.

**Deployment Patterns**:
The MCP server webhook endpoint is accessible at the same host/port as the MCP server itself. Administrators configure Nextcloud to POST to `https://<mcp-server-host>:<port>/webhooks/nextcloud` when registering webhook listeners.

## Decision

We will add a webhook endpoint to the MCP server that receives change notifications from Nextcloud and queues documents for vector database processing. This complements the existing polling architecture from ADR-007 without replacing it—webhooks provide low-latency updates when configured, while polling ensures reliability regardless of webhook availability.

The architecture is intentionally simple: the webhook endpoint is just another producer of `DocumentTask` objects that feed into the existing processor queue. The scanner task, processor pool, and queue management remain unchanged from ADR-007.

### Architecture Components

**1. Webhook Endpoint**

A new Starlette HTTP route will be added to receive webhook notifications from Nextcloud:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

@app.route("/webhooks/nextcloud", methods=["POST"])
async def handle_nextcloud_webhook(request: Request) -> JSONResponse:
    """
    Receive webhook notifications from Nextcloud.

    Parses event payload, extracts document metadata, and queues
    changed documents for processing using the same queue as the scanner.
    """
    # 1. Optional authentication validation
    if settings.webhook_secret:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer ") or \
           auth_header[7:] != settings.webhook_secret:
            logger.warning("Webhook authentication failed")
            return JSONResponse(
                {"status": "error", "message": "Unauthorized"},
                status_code=401
            )

    # 2. Parse webhook payload
    payload = await request.json()
    event_class = payload["event"]["class"]
    user_id = payload["user"]["uid"]

    # 3. Extract document metadata from event
    doc_task = extract_document_task(event_class, payload)
    if not doc_task:
        return JSONResponse({"status": "ignored", "reason": "unsupported event"})

    # 4. Send to processor queue (same queue as scanner)
    try:
        await webhook_send_stream.send(doc_task)
        logger.info(f"Queued document from webhook: {doc_task}")
        return JSONResponse({"status": "queued"})
    except Exception as e:
        logger.error(f"Failed to queue webhook document: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=500
        )
```

The endpoint:
- Validates optional authentication via `Authorization: Bearer <secret>` header
- Parses various event types (calendar, files, tables) into `DocumentTask` objects
- Sends to the same processing queue that the scanner uses
- Returns quickly (<50ms) to avoid blocking Nextcloud's webhook workers
- Handles errors gracefully (invalid payload, queue full, etc.)

**2. Webhook Registration Helper (Development Only)**

For development and testing purposes, a helper method will be added to `NextcloudClient` for registering webhooks via the OCS API. This is NOT exposed as an MCP tool—administrators register webhooks manually using Nextcloud's admin interface or the OCS API directly.

```python
class NextcloudClient:
    async def register_webhook(
        self,
        event_type: str,
        uri: str,
        http_method: str = "POST",
        auth_method: str = "none",
        headers: dict[str, str] | None = None,
    ) -> dict:
        """
        Register a webhook with Nextcloud (requires admin credentials).

        Used for development/testing. Production admins should register
        webhooks using Nextcloud's admin UI or occ commands.
        """
        # Implementation uses OCS API: POST /ocs/v2.php/apps/webhook_listeners/api/v1/webhooks
        ...
```

This keeps webhook registration out of the MCP tool surface while providing a convenient API for integration tests.

**3. Event Parsing**

A helper function extracts `DocumentTask` from various Nextcloud event types:

```python
def extract_document_task(event_class: str, payload: dict) -> DocumentTask | None:
    """Extract DocumentTask from webhook event payload."""
    user_id = payload["user"]["uid"]
    event_data = payload["event"]

    # File/Note events
    if "NodeCreatedEvent" in event_class or "NodeWrittenEvent" in event_class:
        # Only process markdown files (notes)
        path = event_data["node"]["path"]
        if not path.endswith(".md"):
            return None
        return DocumentTask(
            user_id=user_id,
            doc_id=event_data["node"]["id"],
            doc_type="note",
            operation="index",
            modified_at=payload["time"],
        )

    # Calendar events
    elif "CalendarObjectCreatedEvent" in event_class or \
         "CalendarObjectUpdatedEvent" in event_class:
        return DocumentTask(
            user_id=user_id,
            doc_id=str(event_data["objectData"]["id"]),
            doc_type="calendar_event",
            operation="index",
            modified_at=event_data["objectData"]["lastmodified"],
        )

    # Deletion events
    elif "NodeDeletedEvent" in event_class or \
         "CalendarObjectDeletedEvent" in event_class:
        # Similar logic for delete operations
        ...

    return None  # Unsupported event type
```

**4. No Changes to Scanner or Processors**

The existing scanner task from ADR-007 continues operating unchanged. It polls Nextcloud on its configured interval (`VECTOR_SYNC_SCAN_INTERVAL`), discovers changed documents, and queues them for processing. The scanner is unaware of webhooks—it simply adds `DocumentTask` objects to the queue.

Similarly, the processor pool continues pulling `DocumentTask` objects from the queue, generating embeddings, and updating Qdrant. Processors don't know or care whether a task came from the scanner or a webhook.

This design keeps concerns separated: webhooks and scanner are independent producers, processors are independent consumers, and the queue mediates between them.

### Configuration

A new optional environment variable controls webhook authentication:

```bash
# Optional: Shared secret for webhook authentication
# If set, webhooks must include "Authorization: Bearer <secret>" header
# If unset, no authentication is required (useful for local development)
WEBHOOK_SECRET=<generate-random-secret>
```

The webhook endpoint is automatically available at `/webhooks/nextcloud` when the MCP server starts. No feature flags or additional configuration needed—if Nextcloud sends webhooks to this endpoint, they will be processed.

**Reducing Polling Frequency**: Administrators who configure webhooks may want to reduce polling frequency to minimize API load while maintaining safety reconciliation scans:

```bash
# Increase scan interval from 1 hour (default) to 24 hours
VECTOR_SYNC_SCAN_INTERVAL=86400
```

This is a manual configuration decision, not automatic—the scanner doesn't adapt based on webhook availability.

### Webhook Event Mapping

The webhook handler maps Nextcloud events to document types:

| Nextcloud Event | Document Type | Operation |
|----------------|---------------|-----------|
| `NodeCreatedEvent` (path: `*/files/*.md`) | `note` | `index` |
| `NodeWrittenEvent` (path: `*/files/*.md`) | `note` | `index` |
| `NodeDeletedEvent` (path: `*/files/*.md`) | `note` | `delete` |
| `CalendarObjectCreatedEvent` | `calendar_event` | `index` |
| `CalendarObjectUpdatedEvent` | `calendar_event` | `index` |
| `CalendarObjectDeletedEvent` | `calendar_event` | `delete` |
| `RowAddedEvent` | `table_row` | `index` |
| `RowUpdatedEvent` | `table_row` | `index` |
| `RowDeletedEvent` | `table_row` | `delete` |

Path filters in webhook registration ensure only relevant files trigger notifications (e.g., exclude `.jpg`, `.mp4` for file events).

### Administrator Setup

Administrators who want to enable webhooks:

1. **Enable webhook_listeners app** in Nextcloud: `occ app:enable webhook_listeners`
2. **Register webhook endpoints** using Nextcloud's OCS API or admin UI:
   - Endpoint: `https://<mcp-server-host>:<port>/webhooks/nextcloud`
   - Events: File created/updated/deleted, Calendar object events, Table row events
   - Filters: Exclude non-content files (images, videos), system directories
   - Optional: Configure `Authorization: Bearer <WEBHOOK_SECRET>` header
3. **Optionally reduce scanner frequency**: Set `VECTOR_SYNC_SCAN_INTERVAL=86400` (24 hours)
4. **Set up webhook workers** (optional): Configure dedicated background job workers for low-latency delivery

Existing deployments continue using polling without any changes. Webhooks are purely additive.

## Consequences

### Benefits

**Reduced Latency**: With webhooks configured, content changes appear in semantic search within seconds to minutes (depending on Nextcloud background job configuration) instead of up to 1 hour. Queries like "What meetings do I have today?" reflect recent calendar updates.

**Lower API Load**: Administrators who configure webhooks can reduce scanner frequency (e.g., 24-hour intervals), eliminating most polling API calls while maintaining safety reconciliation scans. This significantly reduces load on Nextcloud servers.

**Better Scalability**: Webhooks scale better than polling as content volume grows. The system only processes changed documents instead of checking all documents every hour.

**Simple Architecture**: The webhook endpoint is just another producer feeding the existing processor queue. No changes to scanner, processors, or queue management—webhooks integrate cleanly into the existing architecture.

**Improved User Experience**: Lower-latency semantic search feels more responsive and accurate, especially for time-sensitive queries about recent changes.

### Drawbacks

**Manual Configuration**: Administrators must configure webhooks outside the MCP server using Nextcloud's admin tools. This adds setup complexity compared to the zero-configuration polling approach.

**Deployment Requirements**: Webhooks require the MCP server to be reachable from Nextcloud via HTTP(S). Deployments behind NAT or with restrictive firewalls may not support webhooks without additional networking configuration.

**Asynchronous Delivery**: Nextcloud processes webhooks via background jobs, introducing delivery latency (typically seconds to minutes). The exact latency depends on background job worker configuration and system load.

**Testing Complexity**: Integration tests cannot rely on immediate webhook delivery due to asynchronous background job processing. Tests must either poll for results or mock webhook delivery directly.

**New Failure Modes**: Webhook endpoint downtime, network issues between Nextcloud and MCP server, webhook notification floods from bulk operations. The system must handle these gracefully.

**Version Dependencies**: The webhook_listeners app requires Nextcloud 30+. Older versions continue using polling exclusively.

### Monitoring and Observability

New metrics track webhook performance:

- `webhook_notifications_received_total{event_type}`: Count of webhook notifications by event type
- `webhook_processing_duration_seconds{event_type}`: Webhook handler latency
- `webhook_errors_total{error_type}`: Failed webhook processing by error type (auth failure, parse error, queue full)

Logs include:
- Successful webhook processing: `Queued document from webhook: DocumentTask(...)`
- Webhook authentication failures: `Webhook authentication failed`
- Parse errors: `Failed to parse webhook payload: ...`
- Unsupported events: `Ignoring webhook for unsupported event: ...`

### Security Considerations

**Optional Authentication**: When `WEBHOOK_SECRET` is configured, webhook requests must include `Authorization: Bearer <WEBHOOK_SECRET>` header. The server validates this before processing to prevent unauthorized document queueing. For local development, authentication can be disabled by leaving `WEBHOOK_SECRET` unset.

**Payload Validation**: Webhook payloads are parsed and validated against expected schemas. Malformed payloads are rejected with 400 Bad Request responses.

**No Scope Enforcement**: Unlike MCP tools, webhooks do not enforce progressive consent or check if users have enabled semantic search. Webhooks queue all document changes—administrators control which events trigger webhooks via Nextcloud filters. This keeps the webhook endpoint simple and stateless.

### Testing Strategy

**Unit Tests**: Test webhook handler logic, event parsing, and authentication validation using mocked payloads:

```python
async def test_webhook_endpoint_parses_note_created_event():
    """Unit test: webhook endpoint extracts DocumentTask from note created event."""
    payload = {
        "user": {"uid": "alice"},
        "time": 1704067200,
        "event": {
            "class": "OCP\\Files\\Events\\Node\\NodeCreatedEvent",
            "node": {"id": "123", "path": "/alice/files/test.md"}
        }
    }
    # Mock send_stream and verify DocumentTask is queued
    ...
```

**Integration Tests (Without Real Webhooks)**: Since Nextcloud processes webhooks asynchronously via background jobs, integration tests should NOT rely on triggering real Nextcloud events and waiting for webhook delivery. Instead, tests should:

1. **Mock webhook delivery**: POST webhook payloads directly to the `/webhooks/nextcloud` endpoint
2. **Verify processing**: Check that documents are queued and eventually appear in Qdrant
3. **Test authentication**: Verify requests without valid auth header are rejected (when `WEBHOOK_SECRET` is set)

```python
async def test_webhook_integration_mocked_delivery():
    """Integration test: webhook handler queues document for processing."""
    # POST webhook payload directly to endpoint (bypass Nextcloud)
    response = await client.post("/webhooks/nextcloud", json=note_created_payload)
    assert response.status_code == 200

    # Wait for processor to handle document
    await asyncio.sleep(2)

    # Verify document appears in Qdrant
    results = await qdrant_client.scroll(...)
    assert len(results[0]) > 0
```

**Manual Testing (Real Webhooks)**: For end-to-end validation with real Nextcloud webhook delivery:

1. Register webhook via OCS API or `NextcloudClient.register_webhook()` helper
2. Configure webhook background job workers for low-latency delivery
3. Trigger Nextcloud events (create note, add calendar event)
4. Monitor MCP server logs for webhook delivery
5. Verify documents appear in Qdrant after background job processing

**Failure Mode Tests**:
- Invalid authentication: Verify 401 response when auth header is missing/incorrect
- Malformed payload: Verify 400 response for invalid JSON or missing required fields
- Unsupported event types: Verify graceful handling (ignored, not error)
- Queue full: Verify 500 response with appropriate error message

### Future Enhancements

**Batch Processing**: Group multiple webhook notifications within a short time window (e.g., 5 seconds) into a single batch before queueing. This reduces processor overhead during bulk operations like importing calendars.

**Webhook Payload Optimization**: For large documents, Nextcloud could be configured to send minimal metadata in webhooks (just user_id, doc_id, doc_type), with processors fetching full content lazily. This reduces webhook payload size and network bandwidth.

**Deduplication Window**: Track recently processed documents (last 5 minutes) to avoid redundant work when webhooks and scanner both detect the same change. The processor can check a simple in-memory cache before fetching document content.

## Appendix A: Manual Webhook Testing Results (2025-01-11)

### Testing Summary

Manual validation of Nextcloud webhook schemas and behavior confirmed that webhooks work as documented with several important findings for implementation. **5 out of 6** webhook types were successfully captured and validated.

**Test Environment:**
- Nextcloud 30+ (Docker compose)
- webhook_listeners app enabled
- Test endpoint: `http://mcp:8000/webhooks/nextcloud`
- Background webhook worker running (60s timeout)

**Results:**
- ✅ NodeCreatedEvent (file creation)
- ✅ NodeWrittenEvent (file update)
- ✅ NodeDeletedEvent (file deletion)
- ✅ CalendarObjectCreatedEvent
- ✅ CalendarObjectUpdatedEvent
- ❌ CalendarObjectDeletedEvent (webhook did not fire - potential Nextcloud bug)

### Critical Implementation Findings

#### 1. Deletion Events Lack `node.id` Field

**Finding:** `NodeDeletedEvent` payloads do NOT include `event.node.id`, only `event.node.path`.

**Example:**
```json
{
  "user": {"uid": "admin", "displayName": "admin"},
  "time": 1762851093,
  "event": {
    "class": "OCP\\Files\\Events\\Node\\NodeDeletedEvent",
    "node": {
      "path": "/admin/files/Notes/Webhooks/Webhook Test Note.md"
      // NOTE: No "id" field present
    }
  }
}
```

**Impact:** The event parser in this ADR's example code assumes `event_data["node"]["id"]` exists for all file events. This will fail for deletions.

**Required Fix:** Check for `id` existence and fall back to path-based identification:

```python
def extract_document_task(event_class: str, payload: dict) -> DocumentTask | None:
    user_id = payload["user"]["uid"]
    event_data = payload["event"]

    # File deletion events - NO node.id field
    if "NodeDeletedEvent" in event_class:
        path = event_data["node"]["path"]
        if not path.endswith(".md"):
            return None
        # Use path-based ID since node.id is unavailable
        return DocumentTask(
            user_id=user_id,
            doc_id=f"path:{path}",  # Prefix to distinguish from numeric IDs
            doc_type="note",
            operation="delete",
            modified_at=payload["time"],
        )

    # File creation/update events - node.id exists
    elif "NodeCreatedEvent" in event_class or "NodeWrittenEvent" in event_class:
        path = event_data["node"]["path"]
        if not path.endswith(".md"):
            return None

        # Check if 'id' exists (should, but be defensive)
        node_id = event_data["node"].get("id")
        if not node_id:
            # Fallback for missing ID
            node_id = f"path:{path}"

        return DocumentTask(
            user_id=user_id,
            doc_id=str(node_id),
            doc_type="note",
            operation="index",
            modified_at=payload["time"],
        )
```

**Qdrant Deletion Strategy:** When deleting by path-based ID, search Qdrant for documents with matching path metadata:

```python
async def delete_document_by_path(user_id: str, path: str):
    """Delete document from Qdrant using path (when ID unavailable)."""
    points = await qdrant.scroll(
        collection_name=collection,
        scroll_filter=Filter(must=[
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
            FieldCondition(key="metadata.path", match=MatchValue(value=path)),
        ]),
    )
    # Delete found points...
```

#### 2. Multiple Webhooks Per Operation

**Finding:** Creating a single note triggers 3-5 separate webhook events in rapid succession:

1. `NodeCreatedEvent` for parent folder (if new)
2. `NodeWrittenEvent` for parent folder
3. `NodeCreatedEvent` for the note file
4. `NodeWrittenEvent` for the note file (sometimes fires twice)

**Impact:** Without deduplication, the processor will fetch and index the same note multiple times within seconds, wasting compute and API quota.

**Solution:** The processor queue should be idempotent. If the same document is queued multiple times, only the latest version needs processing. Implementation options:

1. **Queue-level deduplication:** Before adding to queue, check if a task for the same `(user_id, doc_id)` is already pending. Replace the existing task instead of adding duplicate.

2. **Processor-level deduplication:** Track recently processed documents in a short-lived cache (5 minutes). If a document was just processed, skip redundant fetch unless the `modified_at` timestamp is newer.

3. **Accept duplicates:** Let the processor handle duplicates naturally. Qdrant upserts are idempotent—reindexing with identical content is harmless but wasteful.

**Recommendation:** Implement queue-level deduplication by maintaining a map of pending tasks and replacing duplicates with newer timestamps.

#### 3. Type Discrepancy in `node.id`

**Finding:** Nextcloud documentation specifies `node.id` as type `string`, but actual payloads return `int`:

```json
"node": {
  "id": 437,  // integer, not "437"
  "path": "/admin/files/Notes/Webhooks/Webhook Test Note.md"
}
```

**Impact:** Code that assumes `node.id` is always a string will work but may cause type confusion in strongly-typed languages.

**Solution:** Explicitly convert to string when extracting: `doc_id=str(event_data["node"]["id"])`

#### 4. Calendar Events Have Different ID Field Path

**Finding:** Calendar events store the document ID in a different location than file events:

- **File events:** `event.node.id`
- **Calendar events:** `event.objectData.id`

**Impact:** Event parser must handle different field paths for different event types. The example code in this ADR correctly shows this difference.

**Calendar Event Deletion:** Calendar deletion webhooks did NOT fire during testing. This may be a Nextcloud bug or require specific configuration (e.g., trash bin enabled). Until resolved, calendar deletions will only be detected via periodic scanner runs.

#### 5. Rich Metadata in Calendar Webhooks

**Finding:** Calendar webhook payloads include extensive metadata not present in file webhooks:

```json
{
  "event": {
    "calendarId": 1,
    "calendarData": {
      "id": 1,
      "uri": "personal",
      "{http://calendarserver.org/ns/}getctag": "...",
      "{http://sabredav.org/ns}sync-token": 21,
      // ... many calendar-level properties
    },
    "objectData": {
      "id": 3,
      "uri": "webhook-test-event-001.ics",
      "lastmodified": 1762851169,
      "etag": "\"2b937b7d77dc83c77329dfdb210ba9d0\"",
      "calendarid": 1,
      "size": 297,
      "component": "vevent",
      "classification": 0,
      "uid": "webhook-test-event-001@nextcloud",
      "calendardata": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n...",  // Full iCal
      "{http://nextcloud.com/ns}deleted-at": null
    },
    "shares": []  // Array of sharing info
  }
}
```

**Opportunity:** The full iCal content is available in `objectData.calendardata`. The processor could extract metadata directly from the webhook payload instead of making an additional CalDAV request, reducing API load.

### Updated Event Mapping

Based on testing, the actual webhook behavior:

| Nextcloud Event | Fires? | `node.id`/`objectData.id` Present? | Notes |
|----------------|--------|-------------------------------------|-------|
| `NodeCreatedEvent` | ✅ Yes | ✅ Yes (`int`) | Fires for folders too |
| `NodeWrittenEvent` | ✅ Yes | ✅ Yes (`int`) | Fires 1-2x per operation |
| `NodeDeletedEvent` | ✅ Yes | ❌ **NO** (only `path`) | Critical difference |
| `CalendarObjectCreatedEvent` | ✅ Yes | ✅ Yes (`objectData.id`) | Full iCal included |
| `CalendarObjectUpdatedEvent` | ✅ Yes | ✅ Yes (`objectData.id`) | Full iCal included |
| `CalendarObjectDeletedEvent` | ❌ **DID NOT FIRE** | ❓ Unknown | Possible Nextcloud bug |

### Recommended Implementation Changes

The webhook handler code in this ADR requires these modifications:

1. **Handle missing `node.id` in deletions** (see code example in Finding #1)
2. **Add deduplication logic** to prevent redundant processing from multiple webhooks per operation
3. **Validate field existence** before accessing nested properties (`get()` with defaults)
4. **Log unsupported events** at DEBUG level (not WARNING) to avoid log noise
5. **Add calendar deletion fallback:** Since webhook unreliable, calendar deletions rely on scanner reconciliation
6. **Consider payload optimization:** Extract calendar metadata from webhook payload to reduce CalDAV API calls

### Testing Implications

**Integration Test Strategy:**

The asynchronous nature of Nextcloud webhooks makes real webhook delivery unreliable for automated tests:

- ✅ **DO:** POST webhook payloads directly to `/webhooks/nextcloud` endpoint in tests
- ❌ **DON'T:** Trigger Nextcloud events and wait for webhook delivery
- ✅ **DO:** Test authentication, payload parsing, and queue integration with mocked payloads
- ❌ **DON'T:** Assume webhooks fire immediately or reliably

**Manual Testing Required:**
- Real webhook delivery latency (depends on background job workers)
- Calendar deletion webhook behavior (confirm bug or configuration issue)
- Behavior under high-frequency updates (bulk operations)
- Network failure handling (Nextcloud can't reach MCP server)

### Complete Tested Payload Examples

See `webhook-testing-findings.md` in the repository root for:
- Complete JSON payloads for all tested events
- Detailed schema validation results
- Additional edge cases and observations
- Screenshots of webhook logs

## References

- ADR-007: Background Vector Database Synchronization (polling architecture)
- Nextcloud Documentation: `~/Software/documentation/admin_manual/webhook_listeners/index.rst`
- Nextcloud OCS API: Webhook registration endpoint
- Current scanner implementation: `nextcloud_mcp_server/vector/scanner.py:37`
- Webhook Testing Report: `webhook-testing-findings.md` (2025-01-11)
