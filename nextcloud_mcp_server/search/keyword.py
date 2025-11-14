"""Keyword search algorithm using token-based matching (ADR-001)."""

import logging
from typing import Any

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.search.algorithms import SearchAlgorithm, SearchResult

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
        nextcloud_client: NextcloudClient | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Execute keyword search using token matching.

        Args:
            query: Search query to tokenize and match
            user_id: User ID for filtering
            limit: Maximum results to return
            doc_type: Optional document type filter (currently only "note" supported)
            nextcloud_client: NextcloudClient for fetching documents
            **kwargs: Additional parameters (unused)

        Returns:
            List of SearchResult objects ranked by keyword match score

        Raises:
            ValueError: If nextcloud_client not provided
        """
        if not nextcloud_client:
            raise ValueError("KeywordSearch requires nextcloud_client parameter")

        logger.info(
            f"Keyword search: query='{query}', user={user_id}, "
            f"limit={limit}, doc_type={doc_type}"
        )

        # Tokenize query
        query_tokens = self._process_query(query)
        logger.debug(f"Query tokens: {query_tokens}")

        # Currently only supports notes
        # TODO: Extend to other document types (files, calendar, etc.)
        if doc_type and doc_type != "note":
            logger.warning(
                f"Keyword search not yet implemented for doc_type={doc_type}"
            )
            return []

        # Fetch all notes for the user
        notes = await nextcloud_client.notes.get_notes()
        logger.debug(f"Fetched {len(notes)} notes for keyword search")

        # Score and filter notes
        scored_notes = []
        for note in notes:
            score = self._calculate_score(
                query_tokens,
                note.get("title", ""),
                note.get("content", ""),
            )

            if score > 0:  # Only include matches
                # Extract excerpt with context
                excerpt = self._extract_excerpt(
                    note.get("content", ""),
                    query_tokens,
                    max_length=200,
                )

                scored_notes.append(
                    SearchResult(
                        id=note["id"],
                        doc_type="note",
                        title=note.get("title", "Untitled"),
                        excerpt=excerpt,
                        score=score,
                        metadata={
                            "category": note.get("category", ""),
                            "modified": note.get("modified"),
                        },
                    )
                )

        # Sort by score (descending) and limit
        scored_notes.sort(key=lambda x: x.score, reverse=True)
        results = scored_notes[:limit]

        logger.info(f"Keyword search returned {len(results)} matching notes")
        if results:
            result_details = [
                f"note_{r.id} (score={r.score:.3f}, title='{r.title}')"
                for r in results[:5]
            ]
            logger.debug(f"Top keyword results: {', '.join(result_details)}")

        return results

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
