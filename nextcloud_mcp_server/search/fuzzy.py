"""Fuzzy search algorithm using character overlap matching on Qdrant payload."""

import logging
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchValue

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.search.algorithms import SearchAlgorithm, SearchResult
from nextcloud_mcp_server.vector.qdrant_client import get_qdrant_client

logger = logging.getLogger(__name__)


class FuzzySearchAlgorithm(SearchAlgorithm):
    """Fuzzy search using simple character-based similarity.

    Implements character overlap matching with configurable threshold:
    - Compares character sets between query and text
    - Requires configurable % character overlap to match (default: 70%)
    - Tolerant to typos and minor variations
    """

    def __init__(self, threshold: float = 0.7):
        """Initialize fuzzy search algorithm.

        Args:
            threshold: Minimum character overlap ratio (0-1, default: 0.7)
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"Threshold must be between 0.0 and 1.0, got {threshold}")
        self.threshold = threshold

    @property
    def name(self) -> str:
        return "fuzzy"

    async def search(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        doc_type: str | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Execute fuzzy search using character overlap on Qdrant payload.

        Queries Qdrant for all indexed documents, then scores based on character
        overlap in title and excerpt fields. Returns unverified results - access
        verification should be performed separately at the final output stage.

        Args:
            query: Search query
            user_id: User ID for filtering
            limit: Maximum results to return
            doc_type: Optional document type filter (None = all types)
            **kwargs: Additional parameters (threshold override)

        Returns:
            List of unverified SearchResult objects ranked by character overlap score
        """
        settings = get_settings()
        threshold = kwargs.get("threshold", self.threshold)

        logger.info(
            f"Fuzzy search: query='{query}', user={user_id}, "
            f"limit={limit}, threshold={threshold}, doc_type={doc_type}"
        )

        # Build Qdrant filter
        filter_conditions = [
            FieldCondition(key="user_id", match=MatchValue(value=user_id))
        ]
        if doc_type:
            filter_conditions.append(
                FieldCondition(key="doc_type", match=MatchValue(value=doc_type))
            )

        # Scroll through Qdrant to get all matching documents
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
                with_payload=["doc_id", "doc_type", "title", "excerpt", "chunk_index"],
                with_vectors=False,  # Don't need vectors
            )

            all_points.extend(scroll_result)

            if next_offset is None:
                break
            offset = next_offset

        logger.debug(f"Retrieved {len(all_points)} points from Qdrant for fuzzy search")

        # Deduplicate by (doc_id, doc_type) - keep first chunk
        seen_docs = {}
        for point in all_points:
            doc_id = int(point.payload["doc_id"])
            dtype = point.payload.get("doc_type", "note")
            doc_key = (doc_id, dtype)

            chunk_idx = point.payload.get("chunk_index", 0)
            if doc_key not in seen_docs or chunk_idx == 0:
                seen_docs[doc_key] = point

        logger.debug(f"Deduplicated to {len(seen_docs)} unique documents")

        # Score each document based on fuzzy matches
        scored_results = []
        query_lower = query.lower()

        for doc_key, point in seen_docs.items():
            doc_id, dtype = doc_key
            title = point.payload.get("title", "")
            excerpt = point.payload.get("excerpt", "")

            # Check title match
            title_score = self._calculate_char_overlap(query_lower, title.lower())

            # Check excerpt match
            excerpt_score = self._calculate_char_overlap(query_lower, excerpt.lower())

            # Use best score
            best_score = max(title_score, excerpt_score)

            if best_score >= threshold:
                match_location = "title" if title_score >= excerpt_score else "excerpt"
                scored_results.append(
                    {
                        "doc_id": doc_id,
                        "doc_type": dtype,
                        "title": title,
                        "excerpt": excerpt
                        if excerpt_score >= title_score
                        else f"Title match: {title}",
                        "score": best_score,
                        "match_location": match_location,
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
                    metadata={"match_location": result["match_location"]},
                )
            )

        logger.info(f"Fuzzy search returned {len(final_results)} unverified results")
        if final_results:
            result_details = [
                f"{r.doc_type}_{r.id} (score={r.score:.3f}, title='{r.title}')"
                for r in final_results[:5]
            ]
            logger.debug(f"Top fuzzy results: {', '.join(result_details)}")

        return final_results

    def _calculate_char_overlap(self, query: str, text: str) -> float:
        """Calculate character overlap ratio between query and text.

        Args:
            query: Query string (normalized)
            text: Text to compare (normalized)

        Returns:
            Overlap ratio (0.0-1.0)
        """
        if not query or not text:
            return 0.0

        # Convert to character sets
        query_chars = set(query)
        text_chars = set(text)

        # Calculate overlap
        overlap = query_chars & text_chars
        overlap_ratio = len(overlap) / len(query_chars)

        return overlap_ratio

    def _extract_excerpt(self, content: str, max_length: int = 200) -> str:
        """Extract excerpt from content.

        Args:
            content: Full document content
            max_length: Maximum excerpt length

        Returns:
            Excerpt string
        """
        if not content:
            return ""

        excerpt = content[:max_length].strip()
        if len(content) > max_length:
            excerpt += "..."

        return excerpt
