"""Fuzzy search algorithm using character overlap matching."""

import logging
from typing import Any

from nextcloud_mcp_server.search.algorithms import (
    NextcloudClientProtocol,
    SearchAlgorithm,
    SearchResult,
    get_indexed_doc_types,
)

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
        nextcloud_client: NextcloudClientProtocol | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Execute fuzzy search using character overlap.

        Args:
            query: Search query
            user_id: User ID for filtering
            limit: Maximum results to return
            doc_type: Optional document type filter (currently only "note" supported)
            nextcloud_client: NextcloudClient for fetching documents
            **kwargs: Additional parameters (threshold override)

        Returns:
            List of SearchResult objects ranked by character overlap score

        Raises:
            ValueError: If nextcloud_client not provided
        """
        if not nextcloud_client:
            raise ValueError("FuzzySearch requires nextcloud_client parameter")

        threshold = kwargs.get("threshold", self.threshold)

        logger.info(
            f"Fuzzy search: query='{query}', user={user_id}, "
            f"limit={limit}, threshold={threshold}, doc_type={doc_type}"
        )

        # Get available document types from Qdrant
        indexed_types = await get_indexed_doc_types(user_id)
        logger.debug(f"Indexed document types for user: {indexed_types}")

        # Determine which types to search
        if doc_type:
            # Search specific type if requested
            search_types = [doc_type] if doc_type in indexed_types else []
            if not search_types:
                logger.info(f"Doc type '{doc_type}' not indexed for user {user_id}")
                return []
        else:
            # Search all indexed types
            search_types = list(indexed_types)

        # Fetch documents for each type and score them
        all_documents = []
        for dtype in search_types:
            documents = await self._fetch_documents(nextcloud_client, dtype)
            for doc in documents:
                doc["_doc_type"] = dtype  # Tag with type
            all_documents.extend(documents)

        logger.debug(f"Fetched {len(all_documents)} total documents for fuzzy search")

        # Score and filter documents
        scored_results = []
        query_lower = query.lower()

        for doc in all_documents:
            dtype = doc.get("_doc_type", "note")
            title = doc.get("title", "")
            content = doc.get("content", "")

            # Check title match
            title_score = self._calculate_char_overlap(query_lower, title.lower())

            # Check content match
            content_score = self._calculate_char_overlap(query_lower, content.lower())

            # Use best score
            best_score = max(title_score, content_score)

            if best_score >= threshold:
                # Extract excerpt based on which matched better
                if title_score >= content_score:
                    excerpt = f"Title match: {title}"
                else:
                    excerpt = self._extract_excerpt(content, max_length=200)

                scored_results.append(
                    SearchResult(
                        id=doc["id"],
                        doc_type=dtype,
                        title=title or "Untitled",
                        excerpt=excerpt,
                        score=best_score,
                        metadata={
                            "category": doc.get("category", ""),
                            "modified": doc.get("modified"),
                            "match_location": "title"
                            if title_score >= content_score
                            else "content",
                        },
                    )
                )

        # Sort by score (descending) and limit
        scored_results.sort(key=lambda x: x.score, reverse=True)
        results = scored_results[:limit]

        logger.info(f"Fuzzy search returned {len(results)} matching notes")
        if results:
            result_details = [
                f"note_{r.id} (score={r.score:.3f}, title='{r.title}')"
                for r in results[:5]
            ]
            logger.debug(f"Top fuzzy results: {', '.join(result_details)}")

        return results

    async def _fetch_documents(
        self, nextcloud_client: NextcloudClientProtocol, doc_type: str
    ) -> list[dict[str, Any]]:
        """Fetch documents of a specific type from Nextcloud.

        Args:
            nextcloud_client: Client for API access
            doc_type: Document type to fetch ("note", "file", "calendar", etc.)

        Returns:
            List of document dictionaries with at minimum: id, title, content
        """
        if doc_type == "note":
            return await nextcloud_client.notes.get_notes()
        elif doc_type == "file":
            # Future: fetch files when indexed
            logger.info("File documents not yet supported for fuzzy search")
            return []
        elif doc_type == "calendar":
            # Future: fetch calendar events when indexed
            logger.info("Calendar documents not yet supported for fuzzy search")
            return []
        else:
            logger.warning(f"Unknown document type '{doc_type}' for fuzzy search")
            return []

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
