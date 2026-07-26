"""Unified Anthropic provider for text generation."""

import logging

import httpx
from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

from .base import Provider

logger = logging.getLogger(__name__)


class AnthropicProvider(Provider):
    """
    Anthropic provider for text generation.

    Supports Claude models via the Anthropic API.
    Note: Anthropic doesn't provide embedding models, only text generation.
    """

    # 120s read / 5s connect is the house convention (ollama.py, openai.py). The
    # Anthropic SDK otherwise defaults to 600s, long enough that a wedged request
    # looks like a hang rather than a failure.
    DEFAULT_TIMEOUT_SECONDS = 120.0
    DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

    def __init__(
        self,
        api_key: str,
        generation_model: str = "claude-3-5-sonnet-20241022",
        timeout: httpx.Timeout | None = None,
    ):
        """
        Initialize Anthropic provider.

        Args:
            api_key: Anthropic API key
            generation_model: Model name (e.g., "claude-3-5-sonnet-20241022")
            timeout: Optional httpx timeout. Defaults to 120s read / 5s connect,
                matching the other providers.
        """
        if timeout is None:
            timeout = httpx.Timeout(
                timeout=self.DEFAULT_TIMEOUT_SECONDS,
                connect=self.DEFAULT_CONNECT_TIMEOUT_SECONDS,
            )
        self.client = AsyncAnthropic(api_key=api_key, timeout=timeout)
        self.model = generation_model

        logger.info("Initialized Anthropic provider (model=%s)", self.model)

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
        block = message.content[0]
        if not isinstance(block, TextBlock):
            raise ValueError(
                f"Expected a text block from Anthropic, got {type(block).__name__}"
            )
        return block.text

    async def close(self) -> None:
        """Close the client (no-op for Anthropic SDK)."""
        pass
