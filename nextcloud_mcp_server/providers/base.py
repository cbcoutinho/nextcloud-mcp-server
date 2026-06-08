"""Unified provider interface for embeddings and text generation."""

import math
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
