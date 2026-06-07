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

        embeddings, _ = await self.embed_batch_with_usage(texts)
        return embeddings

    async def embed_with_usage(self, text: str) -> tuple[list[float], int]:
        """Embed one text, reporting the request's token count."""
        embeddings, tokens = await self.embed_batch_with_usage([text])
        if not embeddings:
            raise RuntimeError(
                "OpenAI embeddings API returned no embedding for model "
                f"{self.embedding_model}"
            )
        return embeddings[0], tokens

    async def embed_batch_with_usage(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int]:
        """Embed multiple texts, summing the API-reported token usage.

        Returns ``(embeddings, total_tokens)`` where ``total_tokens`` sums
        ``response.usage.total_tokens`` across the sub-requests (the unit the
        provider bills on). Used by the usage-metering hooks (Deck #67). Also
        serves the gateway path via :class:`GatewayProvider`.
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        if not texts:
            return [], 0

        # OpenAI supports batches up to 2048, but use smaller batches for safety
        batch_size = 100
        all_embeddings: list[list[float]] = []
        total_tokens = 0

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]

            # Use helper method with retry logic for each batch
            batch_embeddings, batch_tokens = await self._embed_batch_request(batch)
            all_embeddings.extend(batch_embeddings)
            total_tokens += batch_tokens

            # Update dimension if not set
            if self._dimension is None and batch_embeddings:
                self._dimension = len(batch_embeddings[0])
                logger.info(
                    "Detected embedding dimension: %d for model %s",
                    self._dimension,
                    self.embedding_model,
                )

        return all_embeddings, total_tokens

    @_retry_429
    async def _embed_batch_request(
        self, batch: list[str]
    ) -> tuple[list[list[float]], int]:
        """Make a single batch embedding request with retry logic.

        Returns ``(embeddings, token_count)``; ``token_count`` comes from the
        response's ``usage.total_tokens`` and falls back to a char-based
        estimate if the API omits usage.
        """
        assert self.embedding_model is not None  # Type narrowing
        response = await self.client.embeddings.create(
            input=batch,
            model=self.embedding_model,
        )
        # Sort by index to maintain order
        sorted_data = sorted(response.data, key=lambda x: x.index)
        embeddings = [item.embedding for item in sorted_data]

        usage = getattr(response, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        # Guard on numeric type (not just ``is not None``): a real response
        # gives an int, but test doubles / partial responses can surface a
        # non-numeric attribute — fall back to the estimate there.
        tokens = (
            round(total_tokens)
            if isinstance(total_tokens, (int, float))
            else self._estimate_tokens(batch)
        )
        return embeddings, tokens

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
