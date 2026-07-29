"""BM25 sparse embedding provider using FastEmbed."""

import logging
from typing import Any

import anyio
from fastembed import SparseTextEmbedding

logger = logging.getLogger(__name__)


class BM25SparseEmbeddingProvider:
    """
    BM25 sparse embedding provider for hybrid search.

    Uses FastEmbed's BM25 model to generate sparse vectors for keyword-based
    retrieval. These sparse vectors are combined with dense semantic vectors
    in Qdrant using Reciprocal Rank Fusion (RRF) for hybrid search.

    Unlike dense embeddings which have fixed dimensions, sparse embeddings
    have variable-length vectors with (index, value) pairs representing
    term frequencies in the BM25 vocabulary.
    """

    def __init__(self, model_name: str = "Qdrant/bm25"):
        """
        Initialize BM25 sparse embedding provider.

        Args:
            model_name: FastEmbed BM25 model name (default: Qdrant/bm25)
        """
        self.model_name = model_name
        logger.info("Initializing BM25 sparse embedding provider: %s", model_name)

        # Initialize FastEmbed sparse embedding model
        self.model = SparseTextEmbedding(model_name=model_name)
        logger.info("BM25 sparse embedding model loaded: %s", model_name)

    def encode(self, text: str) -> dict[str, Any]:
        """
        Generate BM25 sparse embedding for a single text (synchronous).

        Note: For async contexts, prefer encode_async() to avoid blocking the event loop.

        Args:
            text: Input text to encode

        Returns:
            Dictionary with 'indices' and 'values' keys for Qdrant sparse vector
        """
        # FastEmbed returns a generator, take first result
        sparse_embedding = next(iter(self.model.embed([text])))

        return {
            "indices": sparse_embedding.indices.tolist(),
            "values": sparse_embedding.values.tolist(),
        }

    async def encode_async(self, text: str) -> dict[str, Any]:
        """
        Generate BM25 sparse embedding for a single text (async).

        Runs CPU-bound BM25 encoding in thread pool to avoid blocking the event loop.

        Args:
            text: Input text to encode

        Returns:
            Dictionary with 'indices' and 'values' keys for Qdrant sparse vector
        """

        # Run CPU-bound BM25 encoding in thread pool
        return await anyio.to_thread.run_sync(lambda: self.encode(text))  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    async def encode_batch(self, texts: list[str]) -> list[dict[str, Any]]:
        """
        Generate BM25 sparse embeddings for multiple texts (batched).

        Args:
            texts: List of texts to encode

        Returns:
            List of dictionaries with 'indices' and 'values' for each text
        """

        # Run CPU-bound BM25 encoding in thread pool to avoid blocking event loop
        sparse_embeddings = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
            lambda: list(self.model.embed(texts))
        )

        return [
            {
                "indices": emb.indices.tolist(),
                "values": emb.values.tolist(),
            }
            for emb in sparse_embeddings
        ]


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
