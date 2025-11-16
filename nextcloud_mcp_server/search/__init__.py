"""Search algorithms module for BM25 hybrid search.

This module provides BM25 hybrid search combining:
- Dense semantic vectors (vector similarity via embeddings)
- Sparse BM25 vectors (keyword-based retrieval)

Results are fused using Qdrant's native Reciprocal Rank Fusion (RRF) for
optimal relevance across both semantic and keyword queries.
"""

from nextcloud_mcp_server.search.algorithms import (
    NextcloudClientProtocol,
    SearchAlgorithm,
    SearchResult,
    get_indexed_doc_types,
)
from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm
from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm

__all__ = [
    "NextcloudClientProtocol",
    "SearchAlgorithm",
    "SearchResult",
    "get_indexed_doc_types",
    "SemanticSearchAlgorithm",
    "BM25HybridSearchAlgorithm",
]
