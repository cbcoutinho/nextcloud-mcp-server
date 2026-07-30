"""Amazon Bedrock provider for embeddings."""

import json
import logging
from typing import Any

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

from .base import Provider

logger = logging.getLogger(__name__)


class BedrockProvider(Provider):
    """
    Amazon Bedrock provider for embeddings.

    Uses AWS Bedrock Runtime API with boto3. Supports various model families:
    - Embeddings: amazon.titan-embed-text-v1, amazon.titan-embed-text-v2, cohere.embed-*

    Requires AWS credentials configured via:
    - Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION)
    - AWS credentials file (~/.aws/credentials)
    - IAM role (when running on AWS)
    """

    # Matches ollama.py / openai.py.
    DEFAULT_TIMEOUT_SECONDS = 120
    DEFAULT_CONNECT_TIMEOUT_SECONDS = 5

    def __init__(
        self,
        region_name: str | None = None,
        embedding_model: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ):
        """
        Initialize Bedrock provider.

        Args:
            region_name: AWS region (e.g., "us-east-1"). Defaults to AWS_REGION env var.
            embedding_model: Model ID for embeddings (e.g., "amazon.titan-embed-text-v2:0").
                None disables embeddings.
            aws_access_key_id: AWS access key (optional, uses default credential chain if not provided)
            aws_secret_access_key: AWS secret key (optional, uses default credential chain if not provided)

        Raises:
            ImportError: If boto3 is not installed
        """
        if not BOTO3_AVAILABLE:
            raise ImportError(
                "boto3 is required for Bedrock provider. Install with: pip install boto3"
            )

        self.embedding_model = embedding_model
        self._dimension: int | None = None  # Detected dynamically

        # Initialize bedrock-runtime client.
        #
        # botocore's defaults are 60s connect and 60s read with 3 retries, so a
        # wedged endpoint can hold a request for minutes. Pin the same 120s read
        # / 5s connect the other providers use; retries stay at botocore's
        # default since Bedrock throttling is expected and handled upstream.
        client_kwargs: dict[str, Any] = {
            "config": BotoConfig(
                connect_timeout=self.DEFAULT_CONNECT_TIMEOUT_SECONDS,
                read_timeout=self.DEFAULT_TIMEOUT_SECONDS,
            )
        }
        if region_name:
            client_kwargs["region_name"] = region_name
        if aws_access_key_id:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key:
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key

        self.client = boto3.client("bedrock-runtime", **client_kwargs)

        logger.info(
            "Initialized Bedrock provider in region %s (embedding_model=%s)",
            region_name or "default",
            embedding_model,
        )

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return self.embedding_model is not None

    def _create_embedding_request(self, text: str) -> dict[str, Any]:
        """
        Create model-specific embedding request payload.

        Args:
            text: Input text to embed

        Returns:
            Request payload dict for the embedding model
        """
        if not self.embedding_model:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        # Titan Embed models
        if self.embedding_model.startswith("amazon.titan-embed"):
            return {"inputText": text}

        # Cohere Embed models
        elif self.embedding_model.startswith("cohere.embed"):
            return {"texts": [text], "input_type": "search_document"}

        # Unknown model - try Titan format as default
        else:
            logger.warning(
                "Unknown embedding model format for %s, using Titan format as default",
                self.embedding_model,
            )
            return {"inputText": text}

    def _parse_embedding_response(self, response: dict[str, Any]) -> list[float]:
        """
        Parse model-specific embedding response.

        Args:
            response: Raw response from Bedrock

        Returns:
            Embedding vector as list of floats
        """
        # Titan Embed models
        if self.embedding_model and self.embedding_model.startswith(
            "amazon.titan-embed"
        ):
            return response["embedding"]

        # Cohere Embed models
        elif self.embedding_model and self.embedding_model.startswith("cohere.embed"):
            return response["embeddings"][0]

        # Unknown model - try Titan format as default
        else:
            logger.warning(
                "Unknown embedding response format for %s, trying Titan format",
                self.embedding_model,
            )
            return response.get("embedding", response.get("embeddings", [None])[0])

    async def embed(self, text: str) -> list[float]:
        """
        Generate embedding vector for text.

        Args:
            text: Input text to embed

        Returns:
            Vector embedding as list of floats

        Raises:
            NotImplementedError: If embeddings not enabled (no embedding_model)
            ClientError: If Bedrock API call fails
        """
        embedding, _ = await self.embed_with_usage(text)
        return embedding

    async def embed_with_usage(self, text: str) -> tuple[list[float], int]:
        """Embed one text, reporting the request's token count.

        Titan Embed responses carry ``inputTextTokenCount``; for Cohere /
        unknown models (no token field) this falls back to a char-based
        estimate. Used by the usage-metering hooks (Deck #67).
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        try:
            request_body = self._create_embedding_request(text)

            response = self.client.invoke_model(
                modelId=self.embedding_model,
                body=json.dumps(request_body),
                accept="application/json",
                contentType="application/json",
            )

            response_body = json.loads(response["body"].read())
            embedding = self._parse_embedding_response(response_body)

            token_count = response_body.get("inputTextTokenCount")
            tokens = (
                round(token_count)
                if isinstance(token_count, (int, float))
                else self._estimate_tokens([text])
            )
            return embedding, tokens

        except (BotoCoreError, ClientError) as e:
            logger.error("Bedrock embedding error: %s", e)
            raise

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Note: Current implementation sends requests sequentially.
        Future optimization could use asyncio for concurrent requests.

        Args:
            texts: List of texts to embed

        Returns:
            List of vector embeddings

        Raises:
            NotImplementedError: If embeddings not enabled (no embedding_model)
            ClientError: If Bedrock API call fails
        """
        embeddings, _ = await self.embed_batch_with_usage(texts)
        return embeddings

    async def embed_batch_with_usage(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int]:
        """Embed multiple texts, summing the per-call token counts.

        Bedrock has no batch embedding API, so requests run sequentially and
        the token total is the sum of each call's ``inputTextTokenCount``
        (Titan) or estimate (Cohere/unknown).
        """
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        embeddings: list[list[float]] = []
        total_tokens = 0
        for text in texts:
            embedding, tokens = await self.embed_with_usage(text)
            embeddings.append(embedding)
            total_tokens += tokens
        return embeddings, total_tokens

    async def _detect_dimension(self):
        """
        Detect embedding dimension by generating a test embedding.
        """
        if self._dimension is None and self.supports_embeddings:
            logger.debug(
                "Detecting embedding dimension for model %s...", self.embedding_model
            )
            test_embedding = await self.embed("test")
            self._dimension = len(test_embedding)
            logger.info(
                "Detected embedding dimension: %s for model %s",
                self._dimension,
                self.embedding_model,
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

    async def close(self) -> None:
        """Close the client (no-op for boto3 clients)."""
        pass
