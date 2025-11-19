# Vector Sync UI Guide

This guide covers the browser-based interface for the Nextcloud MCP Server's semantic search and vector synchronization features.

## Overview

The Vector Sync UI (`/app`) provides an interactive interface to test semantic search queries and visualize results from your Nextcloud documents. It exposes the same retrieval capabilities that LLMs use in Retrieval-Augmented Generation (RAG) workflows, powered by Alpine.js for reactive state, htmx for dynamic updates, and Plotly.js for 3D visualization.

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

### Vector Visualization

Interactive search interface with 3D PCA plot of semantic space.

**Search Controls**:
- **Query**: Natural language search (e.g., "health benefits of coffee")
- **Algorithm**: Semantic (Dense) for pure vector search, or BM25 Hybrid (default) combining vectors + keywords
- **Fusion** (Hybrid only): RRF (Reciprocal Rank Fusion) or DBSF (Distribution-Based Score Fusion)
- **Advanced**: Filter by document type, adjust score threshold (0.0-1.0), set result limit (max 100)

**3D Visualization**:

The plot uses Principal Component Analysis (PCA) to reduce 768-dimensional embeddings to 3D. Documents are positioned by semantic similarity with the query point shown in red. Point size and opacity indicate relevance, and the Viridis color scale shows relative scores (yellow = highest match).

**Critical Fix**: Vectors are L2-normalized before PCA to match Qdrant's cosine distance, ensuring query points position accurately near similar documents. Without normalization, magnitude differences cause misleading spatial separation.

**Results List**:

Each result shows document title (clickable link to Nextcloud), excerpt, raw score, relative percentage, and document type. Click "Show Chunk" to view the matched text segment with surrounding context (up to 500 characters before/after).

## Configuration

**Required**:
```bash
VECTOR_SYNC_ENABLED=true
```

**Optional** (for browser-accessible links):
```bash
NEXTCLOUD_PUBLIC_ISSUER_URL=https://your-public-nextcloud-url.com
```

**Admin Access**: Webhooks tab only visible to Nextcloud admins (verified via Provisioning API).

## Use Cases

**Testing Search Queries**: Preview results before they reach LLMs in RAG workflows. Compare semantic vs. hybrid algorithms, verify relevance scores, and validate that correct documents are retrieved. Use chunk context to see exactly which text segments match and why unexpected documents appear.

**Monitoring Indexing**: Track real-time progress after creating or modifying documents. Check if the queue is backing up (high pending count) or confirm the system is idle after bulk imports. Verify documents become searchable immediately after indexing completes.

**Algorithm Comparison**: Pure semantic search excels at conceptual queries and synonyms. BM25 hybrid combines semantic understanding with precise keyword matching for better accuracy on specific terms. Experiment with RRF vs. DBSF fusion for different score distributions.

## Troubleshooting

**Vector Sync Tab Not Visible**: Set `VECTOR_SYNC_ENABLED=true` and restart the server.

**No Search Results**: Check Vector Sync Status to confirm documents are indexed (not just pending). Try broader queries or lower the score threshold in Advanced options. Initial indexing may take time depending on document volume.

**Links to Nextcloud Apps Not Working**: Set `NEXTCLOUD_PUBLIC_ISSUER_URL` to your browser-accessible Nextcloud URL for correct link generation.

## Related Documentation

- [Configuration Guide](../configuration.md) - Environment variables and settings
- [Authentication Modes](../authentication.md) - BasicAuth vs OAuth setup
- [Installation Guide](../installation.md) - Getting started
- [ADR-008: MCP Sampling for RAG](../ADR-008-mcp-sampling-for-rag.md) - Technical details on RAG workflows
