"""Search algorithms module for unified multi-algorithm search.

This module provides a unified interface for different search algorithms:
- Semantic search (vector similarity)
- Keyword search (token-based matching)
- Fuzzy search (character overlap)
- Hybrid search (RRF fusion of multiple algorithms)

All algorithms share the same interface and can be used interchangeably by both
MCP tools and the visualization pane.
"""

from nextcloud_mcp_server.search.algorithms import SearchAlgorithm, SearchResult
from nextcloud_mcp_server.search.fuzzy import FuzzySearchAlgorithm
from nextcloud_mcp_server.search.hybrid import HybridSearchAlgorithm
from nextcloud_mcp_server.search.keyword import KeywordSearchAlgorithm
from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm

__all__ = [
    "SearchAlgorithm",
    "SearchResult",
    "SemanticSearchAlgorithm",
    "KeywordSearchAlgorithm",
    "FuzzySearchAlgorithm",
    "HybridSearchAlgorithm",
]
