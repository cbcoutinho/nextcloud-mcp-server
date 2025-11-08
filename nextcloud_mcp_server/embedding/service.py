"""Embedding service with provider detection."""

import logging
import os

from .base import EmbeddingProvider
from .ollama_provider import OllamaEmbeddingProvider
from .simple_provider import SimpleEmbeddingProvider

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Unified embedding service with automatic provider detection."""

    def __init__(self):
        """Initialize embedding service with auto-detected provider."""
        self.provider = self._detect_provider()

    def _detect_provider(self) -> EmbeddingProvider:
        """
        Auto-detect available embedding provider.

        Checks environment variables in order:
        1. OLLAMA_BASE_URL - Use Ollama provider (production)
        2. OPENAI_API_KEY - Use OpenAI provider (future)
        3. Fallback to SimpleEmbeddingProvider (testing/development)

        Returns:
            Configured embedding provider
        """
        # Ollama provider (production)
        ollama_url = os.getenv("OLLAMA_BASE_URL")
        if ollama_url:
            logger.info(f"Using Ollama embedding provider: {ollama_url}")
            return OllamaEmbeddingProvider(
                base_url=ollama_url,
                model=os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"),
                verify_ssl=os.getenv("OLLAMA_VERIFY_SSL", "true").lower() == "true",
            )

        # OpenAI provider (future implementation)
        # openai_key = os.getenv("OPENAI_API_KEY")
        # if openai_key:
        #     return OpenAIEmbeddingProvider(api_key=openai_key)

        # Fallback to simple provider for development/testing
        logger.warning(
            "No embedding provider configured (OLLAMA_BASE_URL or OPENAI_API_KEY not set). "
            "Using SimpleEmbeddingProvider for testing/development. "
            "For production, configure an external embedding service."
        )
        return SimpleEmbeddingProvider(dimension=384)

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
