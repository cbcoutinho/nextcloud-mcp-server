"""Amazon Bedrock provider for embeddings and text generation."""

import json
import logging
from typing import Any

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

from .base import Provider

logger = logging.getLogger(__name__)


class BedrockProvider(Provider):
    """
    Amazon Bedrock provider supporting both embeddings and text generation.

    Uses AWS Bedrock Runtime API with boto3. Supports various model families:
    - Embeddings: amazon.titan-embed-text-v1, amazon.titan-embed-text-v2, cohere.embed-*
    - Text Generation: anthropic.claude-*, meta.llama3-*, amazon.titan-text-*, mistral.*, etc.

    Requires AWS credentials configured via:
    - Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION)
    - AWS credentials file (~/.aws/credentials)
    - IAM role (when running on AWS)
    """

    def __init__(
        self,
        region_name: str | None = None,
        embedding_model: str | None = None,
        generation_model: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ):
        """
        Initialize Bedrock provider.

        Args:
            region_name: AWS region (e.g., "us-east-1"). Defaults to AWS_REGION env var.
            embedding_model: Model ID for embeddings (e.g., "amazon.titan-embed-text-v2:0").
                None disables embeddings.
            generation_model: Model ID for text generation (e.g., "anthropic.claude-3-sonnet-20240229-v1:0").
                None disables generation.
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
        self.generation_model = generation_model
        self._dimension: int | None = None  # Detected dynamically

        # Initialize bedrock-runtime client
        client_kwargs: dict[str, Any] = {}
        if region_name:
            client_kwargs["region_name"] = region_name
        if aws_access_key_id:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key:
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key

        self.client = boto3.client("bedrock-runtime", **client_kwargs)

        logger.info(
            f"Initialized Bedrock provider in region {region_name or 'default'} "
            f"(embedding_model={embedding_model}, generation_model={generation_model})"
        )

    @property
    def supports_embeddings(self) -> bool:
        """Whether this provider supports embedding generation."""
        return self.embedding_model is not None

    @property
    def supports_generation(self) -> bool:
        """Whether this provider supports text generation."""
        return self.generation_model is not None

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
                f"Unknown embedding model format for {self.embedding_model}, "
                "using Titan format as default"
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
                f"Unknown embedding response format for {self.embedding_model}, "
                "trying Titan format"
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

            return embedding

        except (BotoCoreError, ClientError) as e:
            logger.error(f"Bedrock embedding error: {e}")
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
        if not self.supports_embeddings:
            raise NotImplementedError(
                "Embedding not supported - no embedding_model configured"
            )

        embeddings = []
        for text in texts:
            embedding = await self.embed(text)
            embeddings.append(embedding)
        return embeddings

    async def _detect_dimension(self):
        """
        Detect embedding dimension by generating a test embedding.
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

    def _create_generation_request(
        self, prompt: str, max_tokens: int
    ) -> dict[str, Any]:
        """
        Create model-specific text generation request payload.

        Args:
            prompt: The prompt to generate from
            max_tokens: Maximum tokens to generate

        Returns:
            Request payload dict for the generation model
        """
        if not self.generation_model:
            raise NotImplementedError(
                "Text generation not supported - no generation_model configured"
            )

        # Anthropic Claude models
        if self.generation_model.startswith("anthropic.claude"):
            return {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "messages": [{"role": "user", "content": prompt}],
            }

        # Meta Llama models
        elif self.generation_model.startswith("meta.llama"):
            return {"prompt": prompt, "max_gen_len": max_tokens, "temperature": 0.7}

        # Amazon Titan Text models
        elif self.generation_model.startswith("amazon.titan-text"):
            return {
                "inputText": prompt,
                "textGenerationConfig": {
                    "maxTokenCount": max_tokens,
                    "temperature": 0.7,
                },
            }

        # Mistral models
        elif self.generation_model.startswith("mistral"):
            return {"prompt": prompt, "max_tokens": max_tokens, "temperature": 0.7}

        # Unknown model - try Claude format as default
        else:
            logger.warning(
                f"Unknown generation model format for {self.generation_model}, "
                "using Claude format as default"
            )
            return {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "messages": [{"role": "user", "content": prompt}],
            }

    def _parse_generation_response(self, response: dict[str, Any]) -> str:
        """
        Parse model-specific text generation response.

        Args:
            response: Raw response from Bedrock

        Returns:
            Generated text
        """
        # Anthropic Claude models
        if self.generation_model and self.generation_model.startswith(
            "anthropic.claude"
        ):
            return response["content"][0]["text"]

        # Meta Llama models
        elif self.generation_model and self.generation_model.startswith("meta.llama"):
            return response["generation"]

        # Amazon Titan Text models
        elif self.generation_model and self.generation_model.startswith(
            "amazon.titan-text"
        ):
            return response["results"][0]["outputText"]

        # Mistral models
        elif self.generation_model and self.generation_model.startswith("mistral"):
            return response["outputs"][0]["text"]

        # Unknown model - try common response fields
        else:
            logger.warning(
                f"Unknown generation response format for {self.generation_model}, "
                "trying common fields"
            )
            # Try common response field names
            for field in ["text", "generation", "outputText", "completion"]:
                if field in response:
                    return response[field]
            # Last resort: return JSON string
            return json.dumps(response)

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
            ClientError: If Bedrock API call fails
        """
        if not self.supports_generation:
            raise NotImplementedError(
                "Text generation not supported - no generation_model configured"
            )

        try:
            request_body = self._create_generation_request(prompt, max_tokens)

            response = self.client.invoke_model(
                modelId=self.generation_model,
                body=json.dumps(request_body),
                accept="application/json",
                contentType="application/json",
            )

            response_body = json.loads(response["body"].read())
            text = self._parse_generation_response(response_body)

            return text

        except (BotoCoreError, ClientError) as e:
            logger.error(f"Bedrock generation error: {e}")
            raise

    async def close(self) -> None:
        """Close the client (no-op for boto3 clients)."""
        pass
