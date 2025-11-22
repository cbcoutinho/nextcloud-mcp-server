"""Unified Ollama provider for embeddings and text generation."""

import logging

import httpx

from .base import Provider

logger = logging.getLogger(__name__)


class OllamaProvider(Provider):
    """
    Ollama provider supporting both embeddings and text generation.

    Supports TLS, SSL verification, and automatic model loading.
    """

    def __init__(
        self,
        base_url: str,
        embedding_model: str | None = None,
        generation_model: str | None = None,
        verify_ssl: bool = True,
        timeout: httpx.Timeout | None = None,
    ):
        """
        Initialize Ollama provider.

        Args:
            base_url: Ollama API base URL (e.g., https://ollama.internal.example.com:443)
            embedding_model: Model for embeddings (e.g., "nomic-embed-text"). None disables embeddings.
            generation_model: Model for text generation (e.g., "llama3.2:1b"). None disables generation.
            verify_ssl: Verify SSL certificates (default: True)
            timeout: HTTP timeout configuration
        """
        self.base_url = base_url.rstrip("/")
        self.embedding_model = embedding_model
        self.generation_model = generation_model
        self.verify_ssl = verify_ssl

        if timeout is None:
            timeout = httpx.Timeout(timeout=120, connect=5)

        self.client = httpx.AsyncClient(verify=verify_ssl, timeout=timeout)
        self._dimension: int | None = None  # Detected dynamically for embeddings

        logger.info(
            f"Initialized Ollama provider: {base_url} "
            f"(embedding_model={embedding_model}, generation_model={generation_model}, "
            f"verify_ssl={verify_ssl})"
        )

        # Pre-check and auto-load models
        if embedding_model:
            self._check_model_is_loaded(embedding_model, autoload=True)
        if generation_model:
            self._check_model_is_loaded(generation_model, autoload=True)

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return self.embedding_model is not None

    @property
    def supports_generation(self) -> bool:
        """Whether this provider supports text generation."""
        return self.generation_model is not None

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

        response = await self.client.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.embedding_model, "prompt": text},
        )
        response.raise_for_status()
        return response.json()["embedding"]

    async def embed_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[list[float]]:
        """
        Generate embeddings for multiple texts using Ollama's batch API.

        Uses /api/embed endpoint with array input for efficient batch processing.
        Conservative batch size (32) prevents quality degradation observed in
        Ollama issue #6262 with larger batches.

        Note: Ollama processes batches serially, not in parallel.

        Args:
            texts: List of texts to embed
            batch_size: Maximum texts per batch (default: 32)

        Returns:
            List of vector embeddings

        Raises:
            NotImplementedError: If embeddings not enabled (no embedding_model)
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = await self.client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.embedding_model, "input": batch},
            )
            response.raise_for_status()
            all_embeddings.extend(response.json()["embeddings"])

        return all_embeddings

    async def _detect_dimension(self):
        """
        Detect embedding dimension by generating a test embedding.

        This method queries the model to determine the actual dimension
        instead of relying on hardcoded values.
        """
        if self._dimension is None and self.supports_embeddings:
            logger.debug(
                f"Detecting embedding dimension for model {self.embedding_model}..."
            )
            test_embedding = await self.embed("test")
            self._dimension = len(test_embedding)
            logger.info(
                f"Detected embedding dimension: {self._dimension} "
                f"for model {self.embedding_model}"
            )

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

        response = await self.client.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.generation_model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": 0.7,
                },
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["response"]

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
