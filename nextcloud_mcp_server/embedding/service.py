"""Embedding service with provider detection.

DEPRECATED: This module is maintained for backward compatibility.
New code should use nextcloud_mcp_server.providers.get_provider() directly.
"""

import logging

import anyio

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


async def get_bm25_service() -> BM25SparseEmbeddingProvider:
    """
    Get singleton BM25 sparse embedding service instance.

    Lazily instantiates the singleton off the event loop. The
    ``BM25SparseEmbeddingProvider`` constructor calls
    ``fastembed.SparseTextEmbedding(model_name="Qdrant/bm25")`` which
    downloads ~50 MB of model weights from HuggingFace and loads them
    into memory — observed >5 s wall-clock in production, enough to
    stall the calling thread. The encode methods on the provider
    already offload via ``anyio.to_thread.run_sync``; this routes the
    first-time init through the same path so the event loop stays
    responsive (kubernetes ``/health/live`` httpGet probe in
    particular).

    The singleton is process-wide so the per-pod cost is paid once.
    Subsequent calls hit the warm path and return after a single
    non-blocking await.

    Concurrent first callers race: two coroutines can both observe
    ``_bm25_service is None`` and both enter ``run_sync``. We accept
    the duplicate model load over an ``asyncio.Lock``, which has its
    own cross-loop hazards under anyio TaskGroups (see PR #799 for
    that class of bug). The duplicate is bounded — FastEmbed caches
    the downloaded weights on disk after the first call, so loser(s)
    pick up cheaply.

    Returns:
        Global BM25SparseEmbeddingProvider instance
    """
    global _bm25_service
    if _bm25_service is None:
        _bm25_service = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
            BM25SparseEmbeddingProvider
        )
    return _bm25_service
