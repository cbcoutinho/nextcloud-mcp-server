"""Embedding service with provider detection.

DEPRECATED: This module is maintained for backward compatibility.
New code should use nextcloud_mcp_server.providers.get_provider() directly.
"""

import logging

from nextcloud_mcp_server.providers import get_provider

from .bm25_provider import BM25SparseEmbeddingProvider

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Unified embedding service with automatic provider detection.

    DEPRECATED: This class wraps the new unified provider infrastructure
    for backward compatibility. New code should use
    nextcloud_mcp_server.providers.get_provider() directly.
    """

    def __init__(self):
        """Initialize embedding service with auto-detected provider."""
        self.provider = get_provider()

    async def embed(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.

        Args:
            text: Input text to embed

        Returns:
            Vector embedding as list of floats
        """
        return await self.provider.embed(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of vector embeddings
        """
        return await self.provider.embed_batch(texts)

    def get_dimension(self) -> int:
        """
        Get embedding dimension.

        Returns:
            Vector dimension
        """
        return self.provider.get_dimension()

    async def close(self):
        """Close provider resources."""
        if hasattr(self.provider, "close") and callable(
            getattr(self.provider, "close")
        ):
            close_method = getattr(self.provider, "close")
            await close_method()


# Singleton instance
_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    """
    Get singleton embedding service instance.

    Returns:
        Global EmbeddingService instance
    """
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


# BM25 sparse embedding singleton
_bm25_service: BM25SparseEmbeddingProvider | None = None


def get_bm25_service() -> BM25SparseEmbeddingProvider:
    """
    Get singleton BM25 sparse embedding service instance.

    Returns:
        Global BM25SparseEmbeddingProvider instance
    """
    global _bm25_service
    if _bm25_service is None:
        _bm25_service = BM25SparseEmbeddingProvider()
    return _bm25_service
