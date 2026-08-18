"""Unified Ollama provider for embeddings."""

import logging

import httpx

from ..retry import retry_on_transient
from .base import Provider

logger = logging.getLogger(__name__)

# Read timeout for an /api/embed call, and the character budget that keeps one
# call inside it. See OllamaProvider._iter_batches.
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_BATCH_CHARS = 16_000


def _is_transient(exc: BaseException) -> bool:
    """Whether an httpx error against Ollama is worth retrying.

    Same shape as the OpenAI/Mistral predicates: connection drops and timeouts
    (a restarting Ollama, or one still loading the model into memory) plus
    429 / 5xx. Permanent 4xx — an unknown model, a malformed request — are NOT
    retried; they fail identically every attempt.

    Timeouts ARE retried, matching ``OpenAIProvider`` (which retries
    ``APITimeoutError`` via ``APIConnectionError``). Worth knowing what that
    costs here: Ollama's read timeout is 120s by default, so a document that
    times out deterministically now burns up to 5×120s per outer attempt rather
    than 1. That is bounded — the ingest loop dead-letters it after
    ``VECTOR_SYNC_MAX_INDEX_FAILURES`` rounds — and the char-bounded batching
    above is what stops it happening in the first place.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    # Everything else caught here is an httpx.RequestError: a transport-level
    # failure (timeout, connect, read, protocol) with no response at all.
    return True


# Catch the httpx.HTTPError base (parent of both RequestError and
# HTTPStatusError) and let the predicate decide; permanent errors re-raise
# immediately.
_retry_transient = retry_on_transient(
    httpx.HTTPError,
    should_retry=_is_transient,
    provider_name="Ollama",
    label="transient error",
)


class OllamaProvider(Provider):
    """
    Ollama provider for embeddings.

    Supports TLS, SSL verification, and automatic model loading.
    """

    def __init__(
        self,
        base_url: str,
        embedding_model: str | None = None,
        verify_ssl: bool = True,
        timeout: httpx.Timeout | None = None,
        embedding_dimensions: int | None = None,
        max_batch_chars: int = DEFAULT_MAX_BATCH_CHARS,
    ):
        """
        Initialize Ollama provider.

        Args:
            base_url: Ollama API base URL (e.g., https://ollama.internal.example.com:443)
            embedding_model: Model for embeddings (e.g., "nomic-embed-text"). None disables embeddings.
            verify_ssl: Verify SSL certificates (default: True)
            timeout: HTTP timeout configuration
            embedding_dimensions: Matryoshka output width to request. None (the
                default) leaves the model at its full width.
            max_batch_chars: Character budget for one ``/api/embed`` request.
                See :meth:`_iter_batches` for why this, and not the item count,
                is what bounds the request.

        Note: Ollama honours ``dimensions`` on ``/api/embed`` and truncates
        *and* re-normalises server-side (verified against nomic-embed-text: the
        256-wide result is the 768-wide prefix rescaled by 1/||prefix||, norm
        1.0). It does NOT validate that the model is Matryoshka-trained —
        snowflake-arctic-embed:110m truncates just as happily, with silent
        recall loss. Only set this for a model documented as MRL-capable.
        """
        self.base_url = base_url.rstrip("/")
        self.embedding_model = embedding_model
        self.verify_ssl = verify_ssl
        self.max_batch_chars = max(1, max_batch_chars)

        if timeout is None:
            timeout = httpx.Timeout(timeout=DEFAULT_TIMEOUT_SECONDS, connect=5)

        self.client = httpx.AsyncClient(verify=verify_ssl, timeout=timeout)
        self._dimension: int | None = None  # Detected dynamically for embeddings
        self._requested_dimensions = embedding_dimensions

        logger.info(
            "Initialized Ollama provider: %s "
            "(embedding_model=%s, verify_ssl=%s, requested_dimensions=%s)",
            base_url,
            embedding_model,
            verify_ssl,
            embedding_dimensions,
        )

        # Pre-check and auto-load models
        if embedding_model:
            self._check_model_is_loaded(embedding_model, autoload=True)

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return self.embedding_model is not None

    def _dimension_params(self) -> dict[str, int]:
        """The ``dimensions`` field for the ``/api/embed`` body, when configured.

        Omitted entirely rather than sent as ``None``: Ollama would treat an
        explicit null as a requested width of nothing.
        """
        if self._requested_dimensions is None:
            return {}
        return {"dimensions": self._requested_dimensions}

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
        # Delegate to embed_with_usage so single and batch embeds use the same
        # /api/embed endpoint (the legacy /api/embeddings differs in payload and
        # omits prompt_eval_count). _detect_dimension() and other embed() callers
        # therefore stay consistent with the search/indexing path.
        embedding, _ = await self.embed_with_usage(text)
        return embedding

    def _iter_batches(self, texts: list[str], batch_size: int):
        """Split ``texts`` into requests bounded by BOTH item count and characters.

        The item cap alone is not enough (GH #1345). Ollama embeds a batch
        serially, so one ``/api/embed`` call costs roughly the batch's *total*
        text — at the default 2048-char chunk size a 32-item batch carries up to
        ~65k chars, which a CPU-only instance cannot finish inside the read
        timeout. The document that surfaced this (206 pages, 326 chunks) timed
        out on its very first batch, every round, forever.

        So the character budget is what actually bounds the request, and
        ``batch_size`` stays as a second cap for the quality degradation Ollama
        issue #6262 reports above ~32 inputs.

        A single text longer than the budget is emitted alone rather than
        dropped: splitting it here would silently change what gets embedded, and
        chunk sizing is the caller's business.
        """
        batch: list[str] = []
        chars = 0
        for text in texts:
            # Only overflow a non-empty batch — an oversize lone text must still
            # be emitted, otherwise this never terminates.
            if batch and (
                len(batch) >= batch_size or chars + len(text) > self.max_batch_chars
            ):
                yield batch
                batch, chars = [], 0
            batch.append(text)
            chars += len(text)
        if batch:
            yield batch

    async def embed_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[list[float]]:
        """
        Generate embeddings for multiple texts using Ollama's batch API.

        Uses /api/embed endpoint with array input for efficient batch processing.
        Batches are bounded by ``max_batch_chars`` as well as ``batch_size`` —
        see :meth:`_iter_batches`.

        Note: Ollama processes batches serially, not in parallel.

        Args:
            texts: List of texts to embed
            batch_size: Maximum texts per batch (default: 32)

        Returns:
            List of vector embeddings

        Raises:
            NotImplementedError: If embeddings not enabled (no embedding_model)
        """
        embeddings, _ = await self.embed_batch_with_usage(texts, batch_size=batch_size)
        return embeddings

    async def embed_with_usage(self, text: str) -> tuple[list[float], int]:
        """Embed one text, reporting the request's token count.

        Routes through ``/api/embed`` (which carries ``prompt_eval_count``)
        rather than the legacy ``/api/embeddings`` so a token count is
        available; falls back to a char-based estimate when the field is
        absent. Used by the usage-metering hooks (Deck #67).
        """
        embeddings, tokens = await self.embed_batch_with_usage([text])
        if not embeddings:
            raise RuntimeError(
                "Ollama embeddings API returned no embedding for model "
                f"{self.embedding_model}"
            )
        return embeddings[0], tokens

    async def embed_batch_with_usage(
        self, texts: list[str], batch_size: int = 32
    ) -> tuple[list[list[float]], int]:
        """Embed multiple texts, summing ``prompt_eval_count`` token usage.

        Returns ``(embeddings, total_tokens)``. Ollama's ``/api/embed`` may
        omit ``prompt_eval_count`` (older versions); a char-based estimate is
        used per batch when it does. Batching is bounded by characters as well
        as by ``batch_size`` — see :meth:`_iter_batches`.
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        if not texts:
            return [], 0

        all_embeddings: list[list[float]] = []
        total_tokens = 0
        for batch in self._iter_batches(texts, batch_size):
            data = await self._embed_batch_request(batch)
            all_embeddings.extend(data["embeddings"])

            # Cache the dimension inline (mirrors OpenAI) so it is set via any
            # embed path, not only an explicit _detect_dimension() call. Also
            # where a requested truncation is enforced, so an endpoint that
            # ignored `dimensions` cannot get past the first batch.
            if data["embeddings"]:
                self._record_dimension(len(data["embeddings"][0]))

            # ``prompt_eval_count`` is assumed to be the batch-level total for a
            # multi-input /api/embed call. Ollama's API docs aren't explicit
            # about batch aggregation; if a version reports only the last
            # input's tokens this understates the batch. Unverified against a
            # live instance — Ollama isn't the Cloud billing provider (Mistral
            # is). If it proves last-item-only, switch to per-item requests and
            # sum. The char-based estimate covers versions that omit the field.
            prompt_eval = data.get("prompt_eval_count")
            total_tokens += (
                round(prompt_eval)
                if isinstance(prompt_eval, (int, float))
                else self._estimate_tokens(batch)
            )

        return all_embeddings, total_tokens

    @_retry_transient
    async def _embed_batch_request(self, batch: list[str]) -> dict:
        """One ``/api/embed`` request, retried on transient failures.

        Mirrors ``MistralProvider._embed_batch_request`` /
        ``OpenAIProvider``'s batch helper: the retry sits on the single request
        so a partial batch is never re-sent, and only transient failures are
        retried (see :func:`_is_transient`). Until this existed, Ollama was the
        only embedding provider with no retry layer at all, so a momentary blip
        went straight to the ingest retry loop (GH #1345).
        """
        try:
            response = await self.client.post(
                f"{self.base_url}/api/embed",
                json={
                    "model": self.embedding_model,
                    "input": batch,
                    **self._dimension_params(),
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # An httpx transport error carries an empty message, so the retry
            # decorator's `%r` degrades to `ReadTimeout('')` and tells an
            # operator nothing about WHAT failed (GH #1345). Always name the
            # batch shape and the endpoint; the tuning hint is timeout-specific,
            # because only a timeout is plausibly caused by batch size.
            hint = ""
            if isinstance(exc, httpx.TimeoutException):
                hint = (
                    " The request cost scales with total characters, because "
                    "Ollama embeds a batch serially — lower "
                    f"OLLAMA_EMBED_MAX_BATCH_CHARS (currently "
                    f"{self.max_batch_chars}) before raising "
                    "OLLAMA_EMBED_TIMEOUT."
                )
            logger.warning(
                "Ollama /api/embed failed (%s): %d texts, %d chars, model=%s, "
                "read timeout=%ss.%s",
                type(exc).__name__,
                len(batch),
                sum(len(t) for t in batch),
                self.embedding_model,
                self.client.timeout.read,
                hint,
            )
            raise
        return response.json()

    async def _detect_dimension(self):
        """
        Detect embedding dimension by generating a test embedding.

        This method queries the model to determine the actual dimension
        instead of relying on hardcoded values.
        """
        if self._dimension is None and self.supports_embeddings:
            logger.debug(
                "Detecting embedding dimension for model %s...", self.embedding_model
            )
            # embed() routes through embed_batch_with_usage(), which records the
            # observed width (and enforces any requested truncation) already.
            await self.embed("test")

    def get_dimension(self) -> int:
        """
        Get embedding dimension.

        Returns:
            Vector dimension for the configured embedding model

        Raises:
            NotImplementedError: If embeddings not enabled (no embedding_model)
            RuntimeError: If dimension not detected yet (call _detect_dimension first)
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        if self._dimension is None:
            raise RuntimeError(
                f"Embedding dimension not detected yet for model {self.embedding_model}. "
                "Call _detect_dimension() first or generate an embedding."
            )
        return self._dimension

    def _check_model_is_loaded(self, model: str, autoload: bool = True):
        """
        Check if model is loaded in Ollama, optionally auto-loading it.

        Args:
            model: Model name to check
            autoload: Whether to automatically pull the model if not loaded
        """
        response = httpx.get(f"{self.base_url}/api/tags")
        response.raise_for_status()

        models = [m["name"] for m in response.json().get("models", [])]
        logger.info("Ollama has following models pre-loaded: %s", models)

        if (model not in models) and autoload:
            logger.warning(
                "Model '%s' not yet available in ollama, attempting to pull now...",
                model,
            )
            response = httpx.post(f"{self.base_url}/api/pull", json={"model": model})
            response.raise_for_status()

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()
