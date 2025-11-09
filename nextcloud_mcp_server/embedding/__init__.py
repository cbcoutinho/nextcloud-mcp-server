"""Embedding service package for generating vector embeddings."""

from .service import EmbeddingService, get_embedding_service
from .simple_provider import SimpleEmbeddingProvider

__all__ = ["EmbeddingService", "get_embedding_service", "SimpleEmbeddingProvider"]
