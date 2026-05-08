"""Unified OpenAI provider for embeddings and text generation.

Supports:
- OpenAI's standard API
- GitHub Models API (models.github.ai)
- Any OpenAI-compatible API via base_url override
"""

import logging

from openai import AsyncOpenAI, RateLimitError

from ._retry import retry_on_rate_limit
from .base import Provider

logger = logging.getLogger(__name__)

# OpenAI's RateLimitError is itself a 429-specific class, so the default
# is_rate_limit predicate ("always True") matches the previous behavior.
_retry_429 = retry_on_rate_limit(RateLimitError, provider_name="OpenAI")


# Well-known embedding dimensions for OpenAI models
OPENAI_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    # GitHub Models API uses openai/ prefix
    "openai/text-embedding-3-small": 1536,
    "openai/text-embedding-3-large": 3072,
}


class OpenAIProvider(Provider):
    """
    OpenAI provider supporting both embeddings and text generation.

    Works with:
    - OpenAI's standard API (api.openai.com)
    - GitHub Models API (models.github.ai)
    - Any OpenAI-compatible API (via base_url)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        embedding_model: str | None = None,
        generation_model: str | None = None,
        timeout: float = 120.0,
    ):
        """
        Initialize OpenAI provider.

        Args:
            api_key: OpenAI API key (or GITHUB_TOKEN for GitHub Models)
            base_url: Base URL override (e.g., "https://models.github.ai/inference")
            embedding_model: Model for embeddings (e.g., "text-embedding-3-small").
                            None disables embeddings.
            generation_model: Model for text generation (e.g., "gpt-4o-mini").
                             None disables generation.
            timeout: HTTP timeout in seconds (default: 120)
        """
        self.embedding_model = embedding_model
        self.generation_model = generation_model
        self._dimension: int | None = None

        # Initialize async client
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

        # Try to get known dimension without API call
        if embedding_model and embedding_model in OPENAI_EMBEDDING_DIMENSIONS:
            self._dimension = OPENAI_EMBEDDING_DIMENSIONS[embedding_model]

        logger.info(
            "Initialized OpenAI provider: base_url=%s "
            "(embedding_model=%s, generation_model=%s, dimension=%s)",
            base_url or "default",
            embedding_model,
            generation_model,
            self._dimension,
        )

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return self.embedding_model is not None

    @property
    def supports_generation(self) -> bool:
        """Whether this provider supports text generation."""
        return self.generation_model is not None

    @_retry_429
    async def embed(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.

        Args:
            text: Input text to embed

        Returns:
            Vector embedding as list of floats

        Raises:
            NotImplementedError: If embeddings not enabled (no embedding_model)
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        assert self.embedding_model is not None  # Type narrowing
        response = await self.client.embeddings.create(
            input=text,
            model=self.embedding_model,
        )

        embedding = response.data[0].embedding

        # Update dimension if not set
        if self._dimension is None:
            self._dimension = len(embedding)
            logger.info(
                "Detected embedding dimension: %d for model %s",
                self._dimension,
                self.embedding_model,
            )

        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts using OpenAI's batch API.

        OpenAI supports up to 2048 inputs per request.

        Args:
            texts: List of texts to embed

        Returns:
            List of vector embeddings

        Raises:
            NotImplementedError: If embeddings not enabled (no embedding_model)
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        if not texts:
            return []

        # OpenAI supports batches up to 2048, but use smaller batches for safety
        batch_size = 100
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]

            # Use helper method with retry logic for each batch
            batch_embeddings = await self._embed_batch_request(batch)
            all_embeddings.extend(batch_embeddings)

            # Update dimension if not set
            if self._dimension is None and batch_embeddings:
                self._dimension = len(batch_embeddings[0])
                logger.info(
                    "Detected embedding dimension: %d for model %s",
                    self._dimension,
                    self.embedding_model,
                )

        return all_embeddings

    @_retry_429
    async def _embed_batch_request(self, batch: list[str]) -> list[list[float]]:
        """Make a single batch embedding request with retry logic."""
        assert self.embedding_model is not None  # Type narrowing
        response = await self.client.embeddings.create(
            input=batch,
            model=self.embedding_model,
        )
        # Sort by index to maintain order
        sorted_data = sorted(response.data, key=lambda x: x.index)
        return [item.embedding for item in sorted_data]

    def get_dimension(self) -> int:
        """
        Get embedding dimension.

        Returns:
            Vector dimension for the configured embedding model

        Raises:
            NotImplementedError: If embeddings not enabled (no embedding_model)
            RuntimeError: If dimension not detected yet (call embed first)
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        if self._dimension is None:
            raise RuntimeError(
                f"Embedding dimension not detected yet for model {self.embedding_model}. "
                "Call embed() first or use a known model."
            )
        return self._dimension

    @_retry_429
    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        """
        Generate text from a prompt.

        Args:
            prompt: The prompt to generate from
            max_tokens: Maximum tokens to generate

        Returns:
            Generated text

        Raises:
            NotImplementedError: If generation not enabled (no generation_model)
        """
        if not self.supports_generation:
            raise NotImplementedError(
                "Text generation not supported - no generation_model configured"
            )

        response = await self.client.chat.completions.create(
            model=self.generation_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.7,
        )

        return response.choices[0].message.content or ""

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.close()
