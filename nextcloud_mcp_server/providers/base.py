"""Unified provider interface for embeddings."""

import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageInput:
    """One image to embed: raw bytes plus the MIME type the provider is told."""

    data: bytes
    mime_type: str


@dataclass(frozen=True)
class ImageInputLimits:
    """What the configured image model accepts (ADR-042 §5).

    ``max_bytes`` / ``max_pixels`` of ``None`` mean the provider imposes no
    limit of its own; the worker still downscales to keep requests small.
    ``max_batch`` is the most images one upstream request may carry —
    :meth:`Provider.embed_image_batch` chunks to it, callers need not.
    """

    mime_types: frozenset[str]
    max_bytes: int | None = None
    max_pixels: int | None = None
    max_batch: int = 1


class ImageInputRejected(Exception):
    """The provider named an image input as unusable (ADR-042 §5).

    The ONLY image error the ingest worker treats as permanent: retrying the
    same bytes against the same model cannot succeed, so the image is recorded
    as skipped instead of being retried into the dead-letter set. Providers
    raise it only for a response that identifies the input itself — never for
    throttling, transport errors or a bare 4xx, which must stay retryable.

    ``index`` is the position of the rejected image within the batch passed to
    :meth:`Provider.embed_image_batch`, or ``None`` when the upstream rejected
    a multi-image request without saying which image was at fault.
    """

    def __init__(self, reason: str, index: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.index = index


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

        The check runs on EVERY embed, not only the one that first resolves the
        width. Caching short-circuits after the first call, but the comparison
        does not: a backend that changes width mid-run — a failover to a
        different model behind one endpoint, say — is caught instead of quietly
        mixing widths within a single collection.
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

    # ------------------------------------------------------------------
    # Image embeddings (ADR-042 §8) — optional capability.
    #
    # Every member defaults to "unsupported", so a provider without image
    # support needs no code. A provider that has it sets
    # ``image_embedding_model`` in __init__ and overrides
    # ``confirm_image_embeddings``, ``image_input_limits``,
    # ``get_image_dimension``, ``_embed_image_chunk`` and
    # ``embed_for_image_space``.
    # ------------------------------------------------------------------

    image_embedding_model: str | None = None
    _requested_image_dimensions: int | None = None
    _image_dimension: int | None = None
    _image_confirmed: bool = False

    async def confirm_image_embeddings(self) -> bool:
        """Confirm the configured image model really takes images.

        Image embeddings are enabled on confirmation, never on configuration
        alone: a provider checks its model advertisement or known-model table
        and returns ``True`` only when the model is image-capable at the
        configured width. Implementations cache the result in
        ``_image_confirmed`` so :attr:`supports_image_embeddings` is a cheap
        read afterwards. The default (no image support) is ``False``.
        """
        return False

    @property
    def supports_image_embeddings(self) -> bool:
        """True once :meth:`confirm_image_embeddings` has succeeded."""
        return self._image_confirmed

    @property
    def image_input_limits(self) -> ImageInputLimits | None:
        """Limits of the configured image model, or ``None`` when unsupported."""
        return None

    def get_image_dimension(self) -> int:
        """Vector width of the image embedding space."""
        raise NotImplementedError("Image embeddings not supported by this provider")

    async def embed_for_image_space(self, text: str) -> list[float]:
        """Embed a text query into the *image* model's space.

        Deliberately separate from :meth:`embed`: the image model and the text
        model produce incompatible spaces, so text→image search must use this.
        """
        raise NotImplementedError("Image embeddings not supported by this provider")

    async def _embed_image_chunk(self, images: list[ImageInput]) -> list[list[float]]:
        """Embed at most ``image_input_limits.max_batch`` images in one request.

        Raise :class:`ImageInputRejected` with an index *relative to this
        chunk*; :meth:`embed_image_batch` rebases it onto the caller's batch.
        """
        raise NotImplementedError("Image embeddings not supported by this provider")

    async def embed_image_batch(self, images: list[ImageInput]) -> list[list[float]]:
        """Embed images into the image space, one vector per input, in order.

        Checks every input against :attr:`image_input_limits` before any
        request is sent, then chunks to ``max_batch``:

        - a MIME type the model does not accept raises
          :class:`ImageInputRejected` (permanent for this model);
        - an image over ``max_bytes`` raises ``ValueError``: the caller was
          supposed to downscale first, so this is a bug, not a bad image, and
          it must not be recorded as a skip.
        """
        self._require_image_support()
        limits = self.image_input_limits
        assert limits is not None  # confirmed providers always report limits
        for i, image in enumerate(images):
            if image.mime_type not in limits.mime_types:
                raise ImageInputRejected(
                    f"{image.mime_type} is not accepted by {self.image_embedding_model}",
                    index=i,
                )
            if limits.max_bytes is not None and len(image.data) > limits.max_bytes:
                raise ValueError(
                    f"Image {i} is {len(image.data)} bytes, over the "
                    f"{limits.max_bytes}-byte limit of {self.image_embedding_model}; "
                    "downscale before embedding"
                )
        vectors: list[list[float]] = []
        for start, chunk in _chunked(images, limits.max_batch):
            try:
                vectors.extend(await self._embed_image_chunk(chunk))
            except ImageInputRejected as exc:
                if exc.index is not None:
                    exc.index += start
                raise
        for vector in vectors:
            self._record_image_dimension(len(vector))
        return vectors

    async def embed_image(self, image: ImageInput) -> list[float]:
        """Embed a single image. Thin wrapper over :meth:`embed_image_batch`."""
        return (await self.embed_image_batch([image]))[0]

    def _require_image_support(self) -> None:
        if not self._image_confirmed:
            raise RuntimeError(
                "Image embeddings are not enabled for this provider: "
                "confirm_image_embeddings() has not succeeded"
            )

    def _record_image_dimension(self, observed: int) -> None:
        """Image-space counterpart of :meth:`_record_dimension`.

        A width that differs from the requested ``IMAGE_EMBEDDING_DIMENSIONS``,
        or from the width seen earlier in this process, is fatal: it would
        mix widths inside the collection's ``image`` vector.
        """
        expected = self._requested_image_dimensions or self._image_dimension
        if expected is not None and observed != expected:
            raise RuntimeError(
                f"Image embedding width {observed} does not match the expected "
                f"{expected} for {self.image_embedding_model} "
                "(IMAGE_EMBEDDING_DIMENSIONS or the width seen earlier)."
            )
        if self._image_dimension is None:
            self._image_dimension = observed
            logger.info(
                "Detected image embedding dimension: %d for model %s",
                observed,
                self.image_embedding_model,
            )

    @abstractmethod
    async def close(self) -> None:
        """Close the provider and release resources."""
        pass


def _chunked(
    items: list[ImageInput], size: int
) -> Iterator[tuple[int, list[ImageInput]]]:
    """Yield ``(start_index, chunk)`` pairs of at most ``size`` items."""
    for start in range(0, len(items), size):
        yield start, items[start : start + size]
