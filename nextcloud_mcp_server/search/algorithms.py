"""Base interfaces and data structures for search algorithms."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class SearchResult:
    """A single search result with metadata and score.

    Attributes:
        id: Document ID
        doc_type: Document type (note, file, calendar, contact, etc.)
        title: Document title
        excerpt: Content excerpt showing match context
        score: Relevance score (0.0-1.0, higher is better)
        metadata: Additional algorithm-specific metadata
    """

    id: int
    doc_type: str
    title: str
    excerpt: str
    score: float
    metadata: dict[str, Any] | None = None

    def __post_init__(self):
        """Validate score is in valid range."""
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"Score must be between 0.0 and 1.0, got {self.score}")


class SearchAlgorithm(ABC):
    """Abstract base class for search algorithms.

    All search algorithms must implement the search() method with consistent
    interface, allowing them to be used interchangeably.
    """

    @abstractmethod
    async def search(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        doc_type: str | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """Execute search with the given parameters.

        Args:
            query: Search query string
            user_id: User ID for multi-tenant filtering
            limit: Maximum number of results to return
            doc_type: Optional document type filter (note, file, calendar, etc.)
            **kwargs: Algorithm-specific parameters

        Returns:
            List of SearchResult objects ranked by relevance

        Raises:
            McpError: If search fails or configuration is invalid
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Return algorithm name for identification."""
        pass

    @property
    def supports_scoring(self) -> bool:
        """Whether this algorithm provides meaningful relevance scores.

        Default: True. Override if algorithm doesn't support scoring.
        """
        return True

    @property
    def requires_vector_db(self) -> bool:
        """Whether this algorithm requires vector database.

        Default: False. Override for semantic search.
        """
        return False
