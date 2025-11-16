"""Unified Anthropic provider for text generation."""

import logging

from anthropic import AsyncAnthropic

from .base import Provider

logger = logging.getLogger(__name__)


class AnthropicProvider(Provider):
    """
    Anthropic provider for text generation.

    Supports Claude models via the Anthropic API.
    Note: Anthropic doesn't provide embedding models, only text generation.
    """

    def __init__(self, api_key: str, model: str = "claude-3-5-sonnet-20241022"):
        """
        Initialize Anthropic provider.

        Args:
            api_key: Anthropic API key
            model: Model name (e.g., "claude-3-5-sonnet-20241022")
        """
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

        logger.info(f"Initialized Anthropic provider (model={model})")

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return False

    @property
    def supports_generation(self) -> bool:
        """Whether this provider supports text generation."""
        return True

    async def embed(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.

        Raises:
            NotImplementedError: Anthropic doesn't provide embedding models
        """
        raise NotImplementedError(
            "Embedding not supported by Anthropic - use Ollama or Bedrock for embeddings"
        )

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Raises:
            NotImplementedError: Anthropic doesn't provide embedding models
        """
        raise NotImplementedError(
            "Embedding not supported by Anthropic - use Ollama or Bedrock for embeddings"
        )

    def get_dimension(self) -> int:
        """
        Get embedding dimension.

        Raises:
            NotImplementedError: Anthropic doesn't provide embedding models
        """
        raise NotImplementedError(
            "Embedding not supported by Anthropic - use Ollama or Bedrock for embeddings"
        )

    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        """
        Generate text using Anthropic API.

        Args:
            prompt: The prompt to generate from
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text
        """
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    async def close(self) -> None:
        """Close the client (no-op for Anthropic SDK)."""
        pass
