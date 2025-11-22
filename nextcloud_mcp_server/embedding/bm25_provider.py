"""BM25 sparse embedding provider using FastEmbed."""

import logging
from typing import Any

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
        logger.info(f"Initializing BM25 sparse embedding provider: {model_name}")

        # Initialize FastEmbed sparse embedding model
        self.model = SparseTextEmbedding(model_name=model_name)
        logger.info(f"BM25 sparse embedding model loaded: {model_name}")

    def encode(self, text: str) -> dict[str, Any]:
        """
        Generate BM25 sparse embedding for a single text.

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

    async def encode_batch(self, texts: list[str]) -> list[dict[str, Any]]:
        """
        Generate BM25 sparse embeddings for multiple texts (batched).

        Args:
            texts: List of texts to encode

        Returns:
            List of dictionaries with 'indices' and 'values' for each text
        """
        import anyio

        # Run CPU-bound BM25 encoding in thread pool to avoid blocking event loop
        sparse_embeddings = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
            lambda: list(self.model.embed(texts))
        )

        return [
            {
                "indices": emb.indices.tolist(),
                "values": emb.values.tolist(),
            }
            for emb in sparse_embeddings
        ]
