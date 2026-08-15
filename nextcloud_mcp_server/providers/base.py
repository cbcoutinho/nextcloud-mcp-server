"""Unified provider interface for embeddings."""

import logging
import math
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class Provider(ABC):
    """
    Unified base class for embedding providers.

    Use the ``supports_embeddings`` capability property to determine whether a
    provider is usable before calling ``embed``/``embed_batch``.
    """

    # Dimension bookkeeping shared by every provider that learns its vector
    # width from the wire. Declared as class attributes so the helpers below can
    # be inherited without each subclass having to re-state them; subclasses
    # still assign the instance attributes in their own ``__init__``.
    _dimension: int | None = None
    _requested_dimensions: int | None = None
    # Every embedding provider sets this in __init__; declared here so shared
    # helpers can read it directly rather than via a defensive getattr, which
    # would silently swallow a future rename.
    embedding_model: str | None = None

    def _record_dimension(self, observed: int) -> None:
        """Cache the vector width seen on the wire, enforcing any requested truncation.

        Matryoshka-capable models accept a ``dimensions`` request parameter and
        return a truncated, re-normalised prefix. Endpoints that do NOT support
        it ignore the parameter *silently* and return the model's full width with
        no error — measured 2026-08-15 against the astrolabe embedding gateway on
        all three of its backend paths, and against Ollama for a non-Matryoshka
        model (which truncates blindly instead).

        Letting a mismatch through would size the Qdrant collection from a name
        claiming one width while it holds another, and bill the full vector RAM
        the truncation was meant to avoid. So it is fatal here, where the cause is
        still legible, rather than surfacing as a dimension error at first upsert
        three layers away.
        """
        if (
            self._requested_dimensions is not None
            and observed != self._requested_dimensions
        ):
            raise RuntimeError(
                f"Requested {self._requested_dimensions}-dimensional embeddings "
                f"(EMBEDDING_DIMENSIONS) but the endpoint returned {observed}. "
                "The 'dimensions' parameter was ignored — either the model is not "
                "Matryoshka-capable, or the service does not forward the parameter "
                "to its backend. Unset EMBEDDING_DIMENSIONS to index at the "
                "model's full width."
            )
        if self._dimension is None:
            self._dimension = observed
            logger.info(
                "Detected embedding dimension: %d for model %s",
                observed,
                self.embedding_model,
            )

    @property
    @abstractmethod
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
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

    @staticmethod
    def _estimate_tokens(texts: list[str]) -> int:
        """Best-effort token estimate when a provider returns no usage data.

        Uses a coarse ~4-chars-per-token heuristic so the billable token
        value stays non-zero and monotone with input size for local/dev
        providers (Simple, Ollama without ``prompt_eval_count``). Real
        providers override ``*_with_usage`` to report exact counts.
        """
        return math.ceil(sum(len(t) for t in texts) / 4)

    async def embed_with_usage(self, text: str) -> tuple[list[float], int]:
        """Embed one text and report the request's token count.

        Returns ``(embedding, token_count)``. The default delegates to
        :meth:`embed` and estimates the tokens; providers that surface real
        usage from their embedding response override this. Used by the
        usage-metering hooks (Deck #67) to bill ``tokens_embedded`` by
        tokens rather than by operation count.

        IMPORTANT (recursion invariant): this default calls ``self.embed``. A
        provider that overrides ``embed()`` to delegate to ``embed_with_usage()``
        (to avoid duplicating request logic) MUST also override this method, or
        the two will call each other forever. The shipped providers that use
        that delegation (Bedrock) do override both — keep that pairing.
        """
        embedding = await self.embed(text)
        return embedding, self._estimate_tokens([text])

    async def embed_batch_with_usage(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int]:
        """Embed multiple texts and report the total token count.

        Returns ``(embeddings, token_count)``; the default estimates. See
        :meth:`embed_with_usage`.

        IMPORTANT (recursion invariant): this default calls ``self.embed_batch``.
        A provider that overrides ``embed_batch()`` to delegate to
        ``embed_batch_with_usage()`` (Mistral, OpenAI, Ollama do) MUST also
        override this method, or the two recurse infinitely. Keep the pairing.
        """
        embeddings = await self.embed_batch(texts)
        return embeddings, self._estimate_tokens(texts)

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
    async def close(self) -> None:
        """Close the provider and release resources."""
        pass
