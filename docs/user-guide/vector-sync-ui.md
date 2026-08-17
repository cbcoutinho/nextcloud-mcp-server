# Vector Sync UI Guide

This guide covers the browser-based interface for the Nextcloud MCP Server's vector synchronization features.

## Overview

The Vector Sync UI (`/app`) shows authentication status and monitors indexing of your Nextcloud documents into the vector store, powered by Alpine.js for reactive state and htmx for dynamic updates.

To *query* the index, use the `nc_semantic_search` MCP tool or the `/api/v1/search` endpoint — the in-browser search-and-visualize tab was removed (see [ADR-012](../ADR-012-unified-multi-algorithm-search.md)).

**Supported Apps**: Notes, Files (text/PDF), Calendar (events/tasks), Contacts (CardDAV), and Deck are indexed and searchable.

## Accessing the UI

Navigate to `/app` after authentication:
- **BasicAuth mode**: `http://localhost:8000/app` (uses credentials from environment)
- **OAuth mode**: `http://localhost:8000/app` (redirects to login if not authenticated)

## Tabs

### Welcome Page

Landing page that introduces semantic search and RAG workflows. Shows authentication status, explains how vector embeddings work, and provides feature navigation. Adapts content based on whether `VECTOR_SYNC_ENABLED=true`.

### User Info

Displays authentication details and session information:
- **BasicAuth**: Username, mode badge, Nextcloud host
- **OAuth**: Username, session ID (truncated), background access status, IdP profile, revocation option

### Vector Sync Status

Real-time monitoring of document indexing:
- **Indexed Documents**: Total chunks stored in Qdrant vector database (immediately searchable)
- **Pending Documents**: Queue awaiting embedding processing
- **Status**: "✓ Idle" (green) when up-to-date, "⟳ Syncing" (orange) during processing

Auto-refreshes every 10 seconds via htmx. Check this tab after adding content to verify indexing completion.

## Configuration

**Required**:
```bash
VECTOR_SYNC_ENABLED=true
```

**Optional** (browser-facing Nextcloud URL, used for the OAuth issuer):
```bash
NEXTCLOUD_PUBLIC_ISSUER_URL=https://your-public-nextcloud-url.com
```

Webhook-based sync is configured in Nextcloud's Astrolabe admin settings, not
here — see the [Webhook Management Guide](../webhook-management-guide.md).

## Use Cases

**Monitoring Indexing**: Track real-time progress after creating or modifying documents. Check if the queue is backing up (high pending count) or confirm the system is idle after bulk imports. Verify documents become searchable immediately after indexing completes.

**Verifying Authentication**: Confirm which Nextcloud identity the server is acting as, whether background (offline) access is granted, and revoke it when needed.

## Troubleshooting

**Vector Sync Tab Not Visible**: Set `VECTOR_SYNC_ENABLED=true` and restart the server.

**No Search Results** (via `nc_semantic_search` or `/api/v1/search`): Check Vector Sync Status to confirm documents are indexed (not just pending). Try broader queries or lower the score threshold. Initial indexing may take time depending on document volume.

## Related Documentation

- [Configuration Guide](../configuration.md) - Environment variables and settings
- [Authentication Modes](../authentication.md) - BasicAuth vs OAuth setup
- [Installation Guide](../installation.md) - Getting started
- [ADR-008: MCP Sampling for RAG](../ADR-008-mcp-sampling-for-rag.md) - Technical details on RAG workflows
