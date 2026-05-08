"""Mistral provider for embeddings.

Currently supports embeddings only (``mistral-embed``, 1024-dim). Generation
can be added later if needed; see ADR-015.
"""

import logging
from functools import wraps

import anyio
from mistralai.client import Mistral
from mistralai.client.errors.sdkerror import SDKError

from .base import Provider

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 2.0
MAX_RETRY_DELAY = 60.0


def retry_on_rate_limit(func):
    """Retry on Mistral 429 (rate limit) responses with exponential backoff."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        retry_delay = INITIAL_RETRY_DELAY
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await func(*args, **kwargs)
            except SDKError as e:
                # SDKError carries a status_code attribute populated from the
                # raw response. Only 429 is retryable here.
                status = getattr(e, "status_code", None)
                if status != 429:
                    raise
                last_error = e
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "Mistral rate limit hit (attempt %d/%d), retrying in %.1fs...",
                        attempt,
                        MAX_RETRIES,
                        retry_delay,
                    )
                    await anyio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)

        logger.error("Mistral rate limit exceeded after %d attempts", MAX_RETRIES)
        raise last_error  # type: ignore[misc]

    return wrapper


# Well-known Mistral embedding model dimensions
MISTRAL_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "mistral-embed": 1024,
}

# Conservative chunk size for batch embeddings. Mistral allows large batches,
# but we keep this in line with sibling providers (OpenAI=100, Ollama=32).
BATCH_SIZE = 64


class MistralProvider(Provider):
    """
    Mistral provider — embeddings only.

    Uses the official ``mistralai`` SDK. Lazy dimension detection mirrors the
    OpenAI provider: known models populate the cached dimension at construction
    time; unknown models get their dimension detected on the first ``embed()``
    call.
    """

    def __init__(
        self,
        api_key: str,
        embedding_model: str | None = "mistral-embed",
        base_url: str | None = None,
    ):
        """
        Initialize the Mistral provider.

        Args:
            api_key: Mistral API key.
            embedding_model: Embedding model ID (default: ``mistral-embed``).
                Pass ``None`` to disable embeddings (the provider will then
                support no capabilities, which is mostly useful for tests).
            base_url: Optional base URL override (e.g. proxies, on-prem).
        """
        self.embedding_model = embedding_model
        self._dimension: int | None = None

        self.client = Mistral(api_key=api_key, server_url=base_url)

        if embedding_model and embedding_model in MISTRAL_EMBEDDING_DIMENSIONS:
            self._dimension = MISTRAL_EMBEDDING_DIMENSIONS[embedding_model]

        logger.info(
            "Initialized Mistral provider: base_url=%s, embedding_model=%s, "
            "dimension=%s",
            base_url or "default",
            embedding_model,
            self._dimension,
        )

    @property
    def supports_embeddings(self) -> bool:
        return self.embedding_model is not None

    @property
    def supports_generation(self) -> bool:
        return False

    @retry_on_rate_limit
    async def embed(self, text: str) -> list[float]:
        """Generate an embedding for a single text."""
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        assert self.embedding_model is not None
        response = await self.client.embeddings.create_async(
            model=self.embedding_model,
            inputs=[text],
        )

        if not response.data or response.data[0].embedding is None:
            raise RuntimeError(
                f"Mistral embeddings API returned no embedding for model "
                f"{self.embedding_model}"
            )

        embedding = response.data[0].embedding

        if self._dimension is None:
            self._dimension = len(embedding)
            logger.info(
                "Detected embedding dimension: %d for model %s",
                self._dimension,
                self.embedding_model,
            )

        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts, chunking by ``BATCH_SIZE``."""
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            batch_embeddings = await self._embed_batch_request(batch)
            all_embeddings.extend(batch_embeddings)

            if self._dimension is None and batch_embeddings:
                self._dimension = len(batch_embeddings[0])
                logger.info(
                    "Detected embedding dimension: %d for model %s",
                    self._dimension,
                    self.embedding_model,
                )

        return all_embeddings

    @retry_on_rate_limit
    async def _embed_batch_request(self, batch: list[str]) -> list[list[float]]:
        """Single batch request with rate-limit retry."""
        assert self.embedding_model is not None
        response = await self.client.embeddings.create_async(
            model=self.embedding_model,
            inputs=batch,
        )

        # Defensive: response.data items have Optional fields. Sort by index
        # (default 0 if missing) and reject None embeddings explicitly.
        sorted_data = sorted(response.data or [], key=lambda x: x.index or 0)
        result: list[list[float]] = []
        for item in sorted_data:
            if item.embedding is None:
                raise RuntimeError(
                    f"Mistral embeddings API returned a null embedding for "
                    f"model {self.embedding_model}"
                )
            result.append(item.embedding)

        if len(result) != len(batch):
            raise RuntimeError(
                f"Mistral embeddings API returned {len(result)} embeddings "
                f"for {len(batch)} inputs"
            )
        return result

    def get_dimension(self) -> int:
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        if self._dimension is None:
            raise RuntimeError(
                f"Embedding dimension not detected yet for model "
                f"{self.embedding_model}. Call embed() first or use a known "
                "model."
            )
        return self._dimension

    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        raise NotImplementedError(
            "MistralProvider does not support generation. "
            "Use OpenAI, Anthropic, or Bedrock for text generation."
        )

    async def close(self) -> None:
        # The Mistral SDK manages its own httpx client lifecycle; close it
        # via the SDK's context-manager hook if present, otherwise no-op.
        close = getattr(self.client, "__aexit__", None)
        if close is not None:
            try:
                await close(None, None, None)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("Mistral client close raised; ignoring", exc_info=True)
