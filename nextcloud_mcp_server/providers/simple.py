"""Simple in-process embedding provider for testing.

This provider uses a basic TF-IDF-like approach with feature hashing to generate
deterministic embeddings without requiring external services. Suitable for testing
but not for production use.
"""

import hashlib
import math
import re
from collections import Counter

from .base import Provider


class SimpleProvider(Provider):
    """Simple deterministic embedding provider using feature hashing.

    This implementation:
    - Tokenizes text into words
    - Uses feature hashing to map words to fixed-size vectors
    - Applies TF-IDF-like weighting
    - Normalizes vectors to unit length

    Not suitable for production but good for testing semantic search infrastructure.
    Only supports embeddings, not text generation.
    """

    def __init__(self, dimension: int = 384):
        """Initialize simple embedding provider.

        Args:
            dimension: Embedding dimension (default: 384)
        """
        self.dimension = dimension

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return True

    @property
    def supports_generation(self) -> bool:
        """Whether this provider supports text generation."""
        return False

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
        hash_bytes = hashlib.md5(word.encode()).digest()
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

    async def generate(self, prompt: str, max_tokens: int = 500) -> str:
        """
        Generate text from a prompt.

        Raises:
            NotImplementedError: Simple provider doesn't support text generation
        """
        raise NotImplementedError(
            "Text generation not supported by Simple provider - use Ollama, Anthropic, or Bedrock"
        )

    async def close(self) -> None:
        """Close the provider (no-op for simple provider)."""
        pass
