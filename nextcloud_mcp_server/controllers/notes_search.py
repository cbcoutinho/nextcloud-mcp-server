"""Controller for notes search functionality."""

from typing import Any, AsyncIterable, Dict, List


class NotesSearchController:
    """Handles notes search logic and scoring."""

    async def search_notes(
        self, notes: AsyncIterable[Dict[str, Any]], query: str
    ) -> List[Dict[str, Any]]:
        """
        Search notes using token-based matching with relevance ranking.
        Returns notes sorted by relevance score.
        """
        search_results = []
        query_tokens = self._process_query(query)

        # If empty query after processing, return empty results
        if not query_tokens:
            return []

        # Process and score each note
        async for note in notes:
            title_tokens, content_tokens = self._process_note_content(note)
            score = self._calculate_score(query_tokens, title_tokens, content_tokens)

            # Only include notes with a non-zero score
            if score >= 0.5:
                search_results.append(
                    {
                        "id": note.get("id"),
                        "title": note.get("title"),
                        "category": note.get("category"),
                        "modified": note.get("modified"),
                        "_score": score,  # Include score for sorting
                    }
                )

        # Sort by score in descending order
        search_results.sort(key=lambda x: x["_score"], reverse=True)

        return search_results

    def _process_query(self, query: str) -> List[str]:
        """
        Tokenize and normalize the search query.
        """
        # Convert to lowercase and split into tokens
        tokens = query.lower().split()
        # Filter out very short tokens
        tokens = [token for token in tokens if len(token) > 1]
        return tokens

    def _process_note_content(
        self, note: Dict[str, Any]
    ) -> tuple[List[str], List[str]]:
        """
        Tokenize and normalize note title and content.
        """
        # Process title
        title = note.get("title", "").lower()
        title_tokens = title.split()

        # Process content
        content = note.get("content", "").lower()
        content_tokens = content.split()

        return title_tokens, content_tokens

    def _calculate_score(
        self,
        query_tokens: List[str],
        title_tokens: List[str],
        content_tokens: List[str],
    ) -> float:
        """
        Calculate a relevance score for a note based on query tokens.
        """
        # Constants for weighting
        TITLE_WEIGHT = 3.0
        CONTENT_WEIGHT = 1.0

        score = 0.0

        # Count matches in title
        title_matches = sum(1 for qt in query_tokens if qt in title_tokens)
        if query_tokens:  # Avoid division by zero
            title_match_ratio = title_matches / len(query_tokens)
            score += TITLE_WEIGHT * title_match_ratio

        # Count matches in content
        content_matches = sum(1 for qt in query_tokens if qt in content_tokens)
        if query_tokens:  # Avoid division by zero
            content_match_ratio = content_matches / len(query_tokens)
            score += CONTENT_WEIGHT * content_match_ratio

        # If no tokens matched at all, return zero
        if title_matches == 0 and content_matches == 0:
            return 0.0

        return score
