"""Fuzzy search algorithm using character overlap matching."""

import logging
from typing import Any

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.search.algorithms import SearchAlgorithm, SearchResult

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
        nextcloud_client: NextcloudClient | None = None,
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

        # Currently only supports notes
        if doc_type and doc_type != "note":
            logger.warning(f"Fuzzy search not yet implemented for doc_type={doc_type}")
            return []

        # Fetch all notes for the user
        notes = await nextcloud_client.notes.get_notes()
        logger.debug(f"Fetched {len(notes)} notes for fuzzy search")

        # Score and filter notes
        scored_notes = []
        query_lower = query.lower()

        for note in notes:
            title = note.get("title", "")
            content = note.get("content", "")

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

                scored_notes.append(
                    SearchResult(
                        id=note["id"],
                        doc_type="note",
                        title=title or "Untitled",
                        excerpt=excerpt,
                        score=best_score,
                        metadata={
                            "category": note.get("category", ""),
                            "modified": note.get("modified"),
                            "match_location": "title"
                            if title_score >= content_score
                            else "content",
                        },
                    )
                )

        # Sort by score (descending) and limit
        scored_notes.sort(key=lambda x: x.score, reverse=True)
        results = scored_notes[:limit]

        logger.info(f"Fuzzy search returned {len(results)} matching notes")
        if results:
            result_details = [
                f"note_{r.id} (score={r.score:.3f}, title='{r.title}')"
                for r in results[:5]
            ]
            logger.debug(f"Top fuzzy results: {', '.join(result_details)}")

        return results

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
