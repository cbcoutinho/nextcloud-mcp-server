"""Unified OpenAI provider for embeddings.

Supports:
- OpenAI's standard API
- GitHub Models API (models.github.ai)
- Any OpenAI-compatible API via base_url override
"""

import logging

from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI, Omit, omit

from ..retry import retry_on_transient
from .base import Provider

logger = logging.getLogger(__name__)


def _is_transient(exc: BaseException) -> bool:
    """Whether an OpenAI APIError is transient and worth retrying.

    Covers the failures seen dropping documents during a backend-pod rollover
    (card 309): ``APIConnectionError`` / ``APITimeoutError`` (brief gateway
    unreachability) and 429 / 5xx status errors. Permanent 4xx (auth, bad
    request) are NOT retried — they would fail identically every attempt.
    """
    if isinstance(exc, APIConnectionError):  # incl. APITimeoutError
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


# Catch the APIError base (parent of both APIConnectionError and APIStatusError)
# and let the predicate decide; non-transient errors re-raise immediately.
_retry_transient = retry_on_transient(
    APIError,
    should_retry=_is_transient,
    provider_name="OpenAI",
    label="transient error",
)


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
    OpenAI provider for embeddings.

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
        timeout: float = 120.0,
        embedding_dimensions: int | None = None,
    ):
        """
        Initialize OpenAI provider.

        Args:
            api_key: OpenAI API key (or GITHUB_TOKEN for GitHub Models)
            base_url: Base URL override (e.g., "https://models.github.ai/inference")
            embedding_model: Model for embeddings (e.g., "text-embedding-3-small").
                            None disables embeddings.
            timeout: HTTP timeout in seconds (default: 120)
            embedding_dimensions: Matryoshka output width to request. None (the
                            default) leaves the model at its full width.
        """
        self.embedding_model = embedding_model
        self._dimension: int | None = None
        self._requested_dimensions = embedding_dimensions

        # Initialize async client
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

        # Try to get known dimension without API call. Skipped when a truncation
        # is requested: the static map records each model's FULL width, so
        # trusting it would size the collection wrong. Leaving the dimension
        # unset makes the first embed observe what the endpoint actually
        # returned — which is also what catches an endpoint that ignored the
        # parameter (see Provider._record_dimension).
        if (
            embedding_model
            and embedding_dimensions is None
            and embedding_model in OPENAI_EMBEDDING_DIMENSIONS
        ):
            self._dimension = OPENAI_EMBEDDING_DIMENSIONS[embedding_model]

        logger.info(
            "Initialized OpenAI provider: base_url=%s "
            "(embedding_model=%s, dimension=%s, requested_dimensions=%s)",
            base_url or "default",
            embedding_model,
            self._dimension,
            embedding_dimensions,
        )

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return self.embedding_model is not None

    def _dimensions_arg(self) -> int | Omit:
        """The ``dimensions`` request argument, when an output width is configured.

        The SDK's ``omit`` sentinel drops the key from the request body entirely
        — passing ``None`` would serialise an explicit null, which several
        OpenAI-compatible endpoints reject.
        """
        if self._requested_dimensions is None:
            return omit
        return self._requested_dimensions

    @_retry_transient
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
            dimensions=self._dimensions_arg(),
        )

        embedding = response.data[0].embedding

        self._record_dimension(len(embedding))

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

            if batch_embeddings:
                self._record_dimension(len(batch_embeddings[0]))

        return all_embeddings, total_tokens

    @_retry_transient
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
            dimensions=self._dimensions_arg(),
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

    async def _detect_dimension(self) -> None:
        """Detect the embedding dimension by embedding a probe string.

        Qdrant collection init needs the vector size at startup, before any
        real embed. Models outside OPENAI_EMBEDDING_DIMENSIONS — every model on
        an OpenAI-compatible endpoint (llama.cpp, LM Studio, vLLM, ...) — are
        only knowable by asking the service, so the vector-sync bootstrap calls
        this hook (``vector/qdrant_client.py``) the same way it does for
        Ollama/Bedrock. ``embed()`` caches the dimension it observes.
        """
        if self._dimension is None and self.supports_embeddings:
            logger.debug(
                "Detecting embedding dimension for model %s...", self.embedding_model
            )
            await self.embed("test")

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

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.close()
