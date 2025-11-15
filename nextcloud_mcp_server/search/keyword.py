"""Keyword search algorithm using token-based matching on Qdrant payload (ADR-001)."""

import logging
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.search.algorithms import SearchAlgorithm, SearchResult
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


class KeywordSearchAlgorithm(SearchAlgorithm):
    """Keyword search using token-based matching with weighted scoring.

    Implements token-based search from ADR-001:
    - Title matches weighted 3x higher than content matches
    - Case-insensitive token matching
    - Relevance scoring based on match frequency and location
    """

    # Weighting constants from ADR-001
    TITLE_WEIGHT = 3.0
    CONTENT_WEIGHT = 1.0

    @property
    def name(self) -> str:
        return "keyword"

    async def search(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        doc_type: str | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Execute keyword search using token matching on Qdrant payload.

        Queries Qdrant for all indexed documents, then scores based on token
        matches in title and excerpt fields. Returns unverified results - access
        verification should be performed separately at the final output stage.

        Args:
            query: Search query to tokenize and match
            user_id: User ID for filtering
            limit: Maximum results to return
            doc_type: Optional document type filter (None = all types)
            **kwargs: Additional parameters (unused)

        Returns:
            List of unverified SearchResult objects ranked by keyword match score
        """
        settings = get_settings()

        logger.info(
            f"Keyword search: query='{query}', user={user_id}, "
            f"limit={limit}, doc_type={doc_type}"
        )

        # Tokenize query
        query_tokens = self._process_query(query)
        logger.debug(f"Query tokens: {query_tokens}")

        # Build Qdrant filter
        filter_conditions = [
            FieldCondition(key="user_id", match=MatchValue(value=user_id))
        ]
        if doc_type:
            filter_conditions.append(
                FieldCondition(key="doc_type", match=MatchValue(value=doc_type))
            )

        # Scroll through Qdrant to get all matching documents
        # We need title and excerpt from payload for token matching
        qdrant_client = await get_qdrant_client()
        collection = settings.get_collection_name()

        all_points = []
        offset = None

        # Scroll through all points matching filter
        while True:
            scroll_result, next_offset = await qdrant_client.scroll(
                collection_name=collection,
                scroll_filter=Filter(must=filter_conditions),
                limit=100,  # Batch size
                offset=offset,
                with_payload=[
                    "doc_id",
                    "doc_type",
                    "title",
                    "excerpt",
                    "chunk_index",
                    "total_chunks",
                ],
                with_vectors=False,  # Don't need vectors for keyword search
            )

            all_points.extend(scroll_result)

            if next_offset is None:
                break
            offset = next_offset

        logger.debug(
            f"Retrieved {len(all_points)} points from Qdrant for keyword search"
        )

        # Deduplicate by (doc_id, doc_type) - keep best chunk per document
        seen_docs = {}
        for point in all_points:
            doc_id = int(point.payload["doc_id"])
            dtype = point.payload.get("doc_type", "note")
            doc_key = (doc_id, dtype)

            # Keep first chunk (chunk_index=0) as it has the most relevant content
            chunk_idx = point.payload.get("chunk_index", 0)
            if doc_key not in seen_docs or chunk_idx == 0:
                seen_docs[doc_key] = point

        logger.debug(f"Deduplicated to {len(seen_docs)} unique documents")

        # Score each document based on keyword matches
        scored_results = []
        for doc_key, point in seen_docs.items():
            doc_id, dtype = doc_key
            title = point.payload.get("title", "")
            excerpt = point.payload.get("excerpt", "")

            # Calculate keyword match score
            score = self._calculate_score(query_tokens, title, excerpt)

            if score > 0:  # Only include matches
                scored_results.append(
                    {
                        "doc_id": doc_id,
                        "doc_type": dtype,
                        "title": title,
                        "excerpt": excerpt,
                        "score": score,
                    }
                )

        # Sort by score (descending) and limit
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        top_results = scored_results[:limit]

        # Return unverified results (verification happens at output stage)
        final_results = []
        for result in top_results:
            final_results.append(
                SearchResult(
                    id=result["doc_id"],
                    doc_type=result["doc_type"],
                    title=result["title"],
                    excerpt=result["excerpt"],
                    score=result["score"],
                    metadata={},
                )
            )

        logger.info(f"Keyword search returned {len(final_results)} unverified results")
        if final_results:
            result_details = [
                f"{r.doc_type}_{r.id} (score={r.score:.3f}, title='{r.title}')"
                for r in final_results[:5]
            ]
            logger.debug(f"Top keyword results: {', '.join(result_details)}")

        return final_results

    def _process_query(self, query: str) -> list[str]:
        """Tokenize and normalize query.

        Args:
            query: Raw query string

        Returns:
            List of normalized tokens
        """
        # Convert to lowercase and split into tokens
        tokens = query.lower().split()

        # Filter out very short tokens (optional)
        tokens = [token for token in tokens if len(token) > 1]

        return tokens

    def _calculate_score(
        self, query_tokens: list[str], title: str, content: str
    ) -> float:
        """Calculate relevance score based on token matches.

        Args:
            query_tokens: List of query tokens
            title: Document title
            content: Document content

        Returns:
            Relevance score (0.0-1.0)
        """
        if not query_tokens:
            return 0.0

        # Process title and content
        title_tokens = title.lower().split()
        content_tokens = content.lower().split()

        score = 0.0

        # Count matches in title
        title_matches = sum(1 for qt in query_tokens if qt in title_tokens)
        if query_tokens:  # Avoid division by zero
            title_match_ratio = title_matches / len(query_tokens)
            score += self.TITLE_WEIGHT * title_match_ratio

        # Count matches in content
        content_matches = sum(1 for qt in query_tokens if qt in content_tokens)
        if query_tokens:
            content_match_ratio = content_matches / len(query_tokens)
            score += self.CONTENT_WEIGHT * content_match_ratio

        # Normalize score to 0-1 range
        # Max score would be TITLE_WEIGHT + CONTENT_WEIGHT if all tokens match everywhere
        max_score = self.TITLE_WEIGHT + self.CONTENT_WEIGHT
        normalized_score = min(score / max_score, 1.0)

        return normalized_score

    def _extract_excerpt(
        self, content: str, query_tokens: list[str], max_length: int = 200
    ) -> str:
        """Extract excerpt showing match context.

        Args:
            content: Full document content
            query_tokens: Query tokens to find
            max_length: Maximum excerpt length in characters

        Returns:
            Excerpt string with context around matches
        """
        if not content:
            return ""

        content_lower = content.lower()

        # Find first occurrence of any query token
        first_match_pos = -1
        for token in query_tokens:
            pos = content_lower.find(token)
            if pos != -1:
                if first_match_pos == -1 or pos < first_match_pos:
                    first_match_pos = pos

        if first_match_pos == -1:
            # No matches found, return beginning
            return content[:max_length].strip() + (
                "..." if len(content) > max_length else ""
            )

        # Extract context around match
        start = max(0, first_match_pos - max_length // 2)
        end = min(len(content), first_match_pos + max_length // 2)

        excerpt = content[start:end].strip()

        # Add ellipsis if truncated
        if start > 0:
            excerpt = "..." + excerpt
        if end < len(content):
            excerpt = excerpt + "..."

        return excerpt
