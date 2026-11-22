"""Simple in-process embedding provider for testing.

This provider uses a basic TF-IDF-like approach with feature hashing to generate
deterministic embeddings without requiring external services. Suitable for testing
but not for production use.
"""

import hashlib
import math
import re
from collections import Counter

from .base import ImageInput, ImageInputLimits, Provider

# The only IMAGE_EMBEDDING_MODEL value SimpleProvider confirms (ADR-042 §8).
# SimpleProvider is also the autodetect fallback, so confirming any configured
# model would let a production deployment with a broken provider config index
# meaningless image vectors and look healthy. Tests opt in by name.
SIMPLE_IMAGE_MODEL = "simple-image-hash"

_SIMPLE_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)


def _hash_vector(payload: bytes, dimension: int) -> list[float]:
    """Deterministic unit vector derived from ``payload`` (no meaning, test-only)."""
    stream = hashlib.shake_256(payload).digest(2 * dimension)
    raw = [
        int.from_bytes(stream[i : i + 2], "big") / 32767.5 - 1.0
        for i in range(0, len(stream), 2)
    ]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


class SimpleProvider(Provider):
    """Simple deterministic embedding provider using feature hashing.

    This implementation:
    - Tokenizes text into words
    - Uses feature hashing to map words to fixed-size vectors
    - Applies TF-IDF-like weighting
    - Normalizes vectors to unit length

    Not suitable for production but good for testing semantic search infrastructure.
    """

    def __init__(self, dimension: int = 384, image_embedding_model: str | None = None):
        """Initialize simple embedding provider.

        Args:
            dimension: Embedding dimension (default: 384), shared by the
                text and image spaces.
            image_embedding_model: ``IMAGE_EMBEDDING_MODEL``. Image embeddings
                are confirmed only for :data:`SIMPLE_IMAGE_MODEL`.
        """
        self.dimension = dimension
        self.image_embedding_model = image_embedding_model

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return True

    def _tokenize(self, text: str) -> list[str]:
        """Tokenize text into lowercase words.

        Args:
            text: Input text

        Returns:
            List of lowercase word tokens
        """
        # Simple word tokenization
        text = text.lower()
        words = re.findall(r"\b\w+\b", text)
        return words

    def _hash_word(self, word: str) -> int:
        """Hash word to dimension index.

        Args:
            word: Word to hash

        Returns:
            Index in range [0, dimension)
        """
        hash_bytes = hashlib.md5(word.encode(), usedforsecurity=False).digest()
        hash_int = int.from_bytes(hash_bytes[:4], byteorder="big")
        return hash_int % self.dimension

    def _embed_single(self, text: str) -> list[float]:
        """Generate embedding for single text.

        Args:
            text: Input text

        Returns:
            Normalized embedding vector
        """
        tokens = self._tokenize(text)
        if not tokens:
            return [0.0] * self.dimension

        # Count term frequencies
        term_freq = Counter(tokens)

        # Initialize vector
        vector = [0.0] * self.dimension

        # Apply TF weighting with feature hashing
        for word, count in term_freq.items():
            idx = self._hash_word(word)
            # Simple TF weighting: log(1 + count)
            vector[idx] += math.log1p(count)

        # Normalize to unit length
        norm = math.sqrt(sum(x * x for x in vector))
        if norm > 0:
            vector = [x / norm for x in vector]

        return vector

    async def embed(self, text: str) -> list[float]:
        """Generate embedding vector for text.

        Args:
            text: Input text to embed

        Returns:
            Vector embedding as list of floats
        """
        return self._embed_single(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed

        Returns:
            List of vector embeddings
        """
        return [self._embed_single(text) for text in texts]

    def get_dimension(self) -> int:
        """Get embedding dimension.

        Returns:
            Vector dimension
        """
        return self.dimension

    async def confirm_image_embeddings(self) -> bool:
        """Confirm only the explicit test sentinel (see :data:`SIMPLE_IMAGE_MODEL`)."""
        self._image_confirmed = self.image_embedding_model == SIMPLE_IMAGE_MODEL
        return self._image_confirmed

    @property
    def image_input_limits(self) -> ImageInputLimits | None:
        if self.image_embedding_model != SIMPLE_IMAGE_MODEL:
            return None
        return ImageInputLimits(mime_types=_SIMPLE_IMAGE_MIME_TYPES, max_batch=64)

    def get_image_dimension(self) -> int:
        self._require_image_support()
        return self.dimension

    async def _embed_image_chunk(self, images: list[ImageInput]) -> list[list[float]]:
        return [
            _hash_vector(b"image:" + image.data, self.dimension) for image in images
        ]

    async def embed_for_image_space(self, text: str) -> list[float]:
        self._require_image_support()
        return _hash_vector(b"text:" + text.encode(), self.dimension)

    async def close(self) -> None:
        """Close the provider (no-op for simple provider)."""
        pass
