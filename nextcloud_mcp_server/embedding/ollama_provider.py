"""Ollama embedding provider."""

import logging

import httpx

from .base import EmbeddingProvider

logger = logging.getLogger(__name__)


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Ollama embedding provider with TLS support."""

    def __init__(
        self,
        base_url: str,
        model: str = "nomic-embed-text",
        verify_ssl: bool = True,
    ):
        """
        Initialize Ollama embedding provider.

        Args:
            base_url: Ollama API base URL (e.g., https://ollama.internal.coutinho.io:443)
            model: Embedding model name (default: nomic-embed-text)
            verify_ssl: Verify SSL certificates (default: True)
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.verify_ssl = verify_ssl
        self.client = httpx.AsyncClient(verify=verify_ssl, timeout=30.0)
        self._dimension = 768  # nomic-embed-text default
        logger.info(
            f"Initialized Ollama provider: {base_url} (model={model}, verify_ssl={verify_ssl})"
        )

        self._check_model_is_loaded(autoload=True)

    async def embed(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.

        Args:
            text: Input text to embed

        Returns:
            Vector embedding as list of floats
        """
        response = await self.client.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.model, "prompt": text},
        )
        response.raise_for_status()
        return response.json()["embedding"]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts (batched requests).

        Note: Ollama doesn't have native batch API, so we send requests sequentially.
        For better performance with large batches, consider using asyncio.gather().

        Args:
            texts: List of texts to embed

        Returns:
            List of vector embeddings
        """
        embeddings = []
        for text in texts:
            embedding = await self.embed(text)
            embeddings.append(embedding)
        return embeddings

    def get_dimension(self) -> int:
        """
        Get embedding dimension.

        Returns:
            Vector dimension (768 for nomic-embed-text)
        """
        return self._dimension

    def _check_model_is_loaded(self, autoload: bool = True):
        response = httpx.get(f"{self.base_url}/api/tags")
        response.raise_for_status()

        models = [model["name"] for model in response.json().get("models", [])]
        logger.info("Ollama has following models pre-loaded: %s", models)

        if (self.model not in models) and autoload:
            logger.warning(
                "Embedding model '%s' not yet available in ollama, attempting to pull now...",
                self.model,
            )
            response = httpx.post(
                f"{self.base_url}/api/pull", json={"model": self.model}
            )
            response.raise_for_status()

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
