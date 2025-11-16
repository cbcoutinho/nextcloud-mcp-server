"""Embedding service package for generating vector embeddings."""

from .bm25_provider import BM25SparseEmbeddingProvider
from .service import EmbeddingService, get_bm25_service, get_embedding_service
from .simple_provider import SimpleEmbeddingProvider

__all__ = [
    "EmbeddingService",
    "get_embedding_service",
    "BM25SparseEmbeddingProvider",
    "get_bm25_service",
    "SimpleEmbeddingProvider",
]
