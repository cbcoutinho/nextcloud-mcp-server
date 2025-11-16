"""Unified provider interface for embeddings and text generation."""

from abc import ABC, abstractmethod


class Provider(ABC):
    """
    Unified base class for LLM providers.

    Providers can support embeddings, text generation, or both.
    Use capability properties to determine what features are available.
    """

    @property
    @abstractmethod
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        pass

    @property
    @abstractmethod
    def supports_generation(self) -> bool:
        """Whether this provider supports text generation."""
        pass

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.

        Args:
            text: Input text to embed

        Returns:
            Vector embedding as list of floats

        Raises:
            NotImplementedError: If provider doesn't support embeddings
        """
        pass

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts (optimized).

        Args:
            texts: List of texts to embed

        Returns:
            List of vector embeddings

        Raises:
            NotImplementedError: If provider doesn't support embeddings
        """
        pass

    @abstractmethod
    def get_dimension(self) -> int:
        """
        Get embedding dimension for this provider.

        Returns:
            Vector dimension (e.g., 768 for nomic-embed-text)

        Raises:
            NotImplementedError: If provider doesn't support embeddings
        """
        pass

    @abstractmethod
    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        """
        Generate text from a prompt.

        Args:
            prompt: The prompt to generate from
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text

        Raises:
            NotImplementedError: If provider doesn't support generation
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close the provider and release resources."""
        pass
