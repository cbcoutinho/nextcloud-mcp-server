"""Mistral provider for embeddings.

Currently supports embeddings only (``mistral-embed``, 1024-dim). Generation
can be added later if needed; see ADR-015.
"""

import logging

# mistralai 2.x ships no top-level __init__.py, so `from mistralai import …`
# raises ImportError. The canonical public paths are `mistralai.client` (which
# re-exports the SDK class via `client/__init__.py`) and `mistralai.client.errors`
# (which lazy-loads SDKError). There is no `mistralai.models` subpackage either.
from mistralai.client import Mistral
from mistralai.client.errors import SDKError

from ._retry import retry_on_transient
from .base import Provider

logger = logging.getLogger(__name__)

# Well-known Mistral embedding model dimensions
MISTRAL_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "mistral-embed": 1024,
}

# Conservative chunk size for batch embeddings. Mistral allows large batches,
# but we keep this in line with sibling providers (OpenAI=100, Ollama=32).
BATCH_SIZE = 64

# Per-request timeout (milliseconds) applied to embedding calls.
#
# NOTE: We pass this explicitly on every request instead of configuring a
# timeout on an injected httpx client, because the mistralai SDK ignores
# client-level timeouts for embeddings. Its generated ``embeddings.create*``
# methods hard-default ``timeout_ms`` (60_000) and feed that scalar straight
# into ``httpx.build_request(timeout=...)``, which *replaces* any
# ``httpx.Timeout`` configured on the client. Consequently an injected
# ``AsyncClient(timeout=...)`` is silently dropped, and a distinct ``connect``
# timeout cannot be expressed at all (the SDK exposes only a single scalar).
#   - Upstream: mistralai/client-python#449 (the original "SDK passes
#     timeout=None and overrides the client" hang; fixed in v2.3.0) plus the
#     residual per-method-default override discussed there and tracked via
#     #474.
#   - We pin mistralai 2.4.5 on purpose (2.4.6 was a supply-chain compromise,
#     #523), so setting the bound explicitly here keeps it intentional and
#     independent of the SDK's internal default.
_EMBED_TIMEOUT_MS = 60_000

_NO_EMBEDDING_MODEL_MSG = "Embedding not supported - no embedding_model configured"


def _is_transient(exc: BaseException) -> bool:
    """Retry HTTP 429 (rate limit) and 5xx (server/transient) SDKErrors.

    Scope is deliberately SDK-level: only ``SDKError`` (an HTTP-status error) is
    caught by the decorator, so a pure connection drop that the Mistral SDK
    surfaces as a bare ``httpx``/``ConnectionError`` is NOT retried here. The
    primary pod-rollover resilience target (card 309) is the gateway path via
    the OpenAI-compatible client, which does cover connection errors; direct
    Mistral is a self-hoster fallback where 429/5xx is the common transient.
    """
    status = getattr(exc, "status_code", None)
    return status == 429 or (isinstance(status, int) and status >= 500)


_retry_transient = retry_on_transient(
    SDKError,
    should_retry=_is_transient,
    provider_name="Mistral",
    label="transient error",
)


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

    @_retry_transient
    async def embed(self, text: str) -> list[float]:
        """Generate an embedding for a single text."""
        if not self.supports_embeddings:
            raise NotImplementedError(_NO_EMBEDDING_MODEL_MSG)

        assert self.embedding_model is not None
        response = await self.client.embeddings.create_async(
            model=self.embedding_model,
            inputs=[text],
            timeout_ms=_EMBED_TIMEOUT_MS,
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
        embeddings, _ = await self.embed_batch_with_usage(texts)
        return embeddings

    async def embed_with_usage(self, text: str) -> tuple[list[float], int]:
        """Embed one text, reporting the Mistral request's token count."""
        embeddings, tokens = await self.embed_batch_with_usage([text])
        if not embeddings:
            raise RuntimeError(
                f"Mistral embeddings API returned no embedding for model "
                f"{self.embedding_model}"
            )
        return embeddings[0], tokens

    async def embed_batch_with_usage(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int]:
        """Embed multiple texts, summing the Mistral-reported token usage.

        Returns ``(embeddings, total_tokens)`` where ``total_tokens`` is the
        sum of ``response.usage.total_tokens`` across the ``BATCH_SIZE`` sub-
        requests (the unit Mistral bills on). Used by the usage-metering hooks
        to record ``tokens_embedded`` by tokens (Deck #67).
        """
        if not self.supports_embeddings:
            raise NotImplementedError(_NO_EMBEDDING_MODEL_MSG)

        if not texts:
            return [], 0

        all_embeddings: list[list[float]] = []
        total_tokens = 0
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i : i + BATCH_SIZE]
            batch_embeddings, batch_tokens = await self._embed_batch_request(batch)
            all_embeddings.extend(batch_embeddings)
            total_tokens += batch_tokens

            if self._dimension is None and batch_embeddings:
                self._dimension = len(batch_embeddings[0])
                logger.info(
                    "Detected embedding dimension: %d for model %s",
                    self._dimension,
                    self.embedding_model,
                )

        return all_embeddings, total_tokens

    @_retry_transient
    async def _embed_batch_request(
        self, batch: list[str]
    ) -> tuple[list[list[float]], int]:
        """Single batch request with rate-limit retry.

        Returns ``(embeddings, token_count)``; ``token_count`` comes from the
        response's ``usage.total_tokens`` and falls back to a char-based
        estimate if the API omits usage.
        """
        assert self.embedding_model is not None
        response = await self.client.embeddings.create_async(
            model=self.embedding_model,
            inputs=batch,
            timeout_ms=_EMBED_TIMEOUT_MS,
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
        return result, tokens

    def get_dimension(self) -> int:
        if not self.supports_embeddings:
            raise NotImplementedError(_NO_EMBEDDING_MODEL_MSG)

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
        # The mistralai 2.x client (Speakeasy-generated) does not expose a
        # public close()/aclose() — only the async-context-manager protocol
        # (__aenter__/__aexit__). Calling __aexit__ directly is internal API
        # and brittle across SDK patch versions; the underlying httpx client
        # is closed during garbage collection, so we leave this as a no-op.
        return None
