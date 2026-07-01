"""Amazon Bedrock provider for embeddings and text generation."""

import base64
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

# Cohere Embed v4 documents up to 96 images per /v2/embed call; chunk well
# under that to leave headroom for serialization and avoid 400s on edge sizes.
_COHERE_IMAGE_BATCH_SIZE = 64


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
        image_embedding_model: str | None = None,
        image_output_dim: int = 1024,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ):
        """
        Initialize Bedrock provider.

        Args:
            region_name: AWS region (e.g., "us-east-1"). Defaults to AWS_REGION env var.
            embedding_model: Model ID for text embeddings (e.g., "amazon.titan-embed-text-v2:0").
                None disables text embeddings.
            generation_model: Model ID for text generation (e.g., "anthropic.claude-3-sonnet-20240229-v1:0").
                None disables generation.
            image_embedding_model: Model ID for joint text-image embeddings
                (e.g., "amazon.titan-embed-image-v1", "cohere.embed-v4:0").
                None disables image embeddings.
            image_output_dim: Output dimension for Titan Multimodal G1 (256, 384, or 1024).
                Ignored by Cohere and other models.
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
        self.image_embedding_model = image_embedding_model
        self.image_output_dim = image_output_dim
        self._dimension: int | None = None  # Detected dynamically
        self._image_dimension: int | None = None  # Detected on first image embed

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
            "Initialized Bedrock provider in region %s "
            "(embedding_model=%s, generation_model=%s, image_embedding_model=%s)",
            region_name or "default",
            embedding_model,
            generation_model,
            image_embedding_model,
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

    @property
    def supports_image_embeddings(self) -> bool:
        return self.image_embedding_model is not None

    def _create_image_embedding_request(
        self,
        *,
        image_b64s: list[str] | None = None,
        text: str | None = None,
        mime_type: str = "image/jpeg",
        cohere_input_type: str = "search_document",
    ) -> dict[str, Any]:
        """Build the Bedrock invoke_model body for the configured image model.

        For Cohere, pass 1+ images via ``image_b64s`` (the API natively batches).
        For Titan G1, ``image_b64s`` must have length ≤1 — it only accepts a
        single image per call.
        """
        if not self.image_embedding_model:
            raise NotImplementedError(
                "Image embeddings not supported - no image_embedding_model configured"
            )

        if self.image_embedding_model.startswith("amazon.titan-embed-image"):
            if image_b64s and len(image_b64s) > 1:
                raise ValueError(
                    "Titan Multimodal G1 accepts only one image per call; "
                    "callers must iterate via embed_image()"
                )
            body: dict[str, Any] = {
                "embeddingConfig": {"outputEmbeddingLength": self.image_output_dim},
            }
            if image_b64s:
                body["inputImage"] = image_b64s[0]
            if text is not None:
                body["inputText"] = text
            return body

        if self.image_embedding_model.startswith("cohere.embed"):
            body = {
                "input_type": cohere_input_type,
                "embedding_types": ["float"],
            }
            if image_b64s:
                body["images"] = [f"data:{mime_type};base64,{b}" for b in image_b64s]
            if text is not None:
                body["texts"] = [text]
            return body

        raise ValueError(
            f"Unsupported image embedding model: {self.image_embedding_model}"
        )

    def _parse_image_embedding_response(
        self, response: dict[str, Any]
    ) -> list[list[float]]:
        """Return the list of vectors from a multimodal embedding response.

        Always returns a list (length 1 for single-input models like Titan).
        """
        model = self.image_embedding_model or ""
        if model.startswith("amazon.titan-embed-image"):
            if response.get("message"):
                raise RuntimeError(
                    f"Titan multimodal embedding error: {response['message']}"
                )
            return [response["embedding"]]
        if model.startswith("cohere.embed"):
            return response["embeddings"]["float"]
        raise ValueError(f"Unsupported image embedding model: {model}")

    def _invoke_image_model(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.image_embedding_model:
            raise NotImplementedError(
                "Image embeddings not supported - no image_embedding_model configured"
            )
        try:
            response = self.client.invoke_model(
                modelId=self.image_embedding_model,
                body=json.dumps(body),
                accept="application/json",
                contentType="application/json",
            )
            return json.loads(response["body"].read())
        except (BotoCoreError, ClientError) as e:
            logger.error("Bedrock image embedding error: %s", e)
            raise

    def _remember_image_dim(self, vector: list[float]) -> None:
        if self._image_dimension is None:
            self._image_dimension = len(vector)
            logger.info(
                "Detected image embedding dimension: %s for model %s",
                self._image_dimension,
                self.image_embedding_model,
            )

    async def embed_image(
        self, image: bytes, mime_type: str = "image/jpeg"
    ) -> list[float]:
        if not self.supports_image_embeddings:
            raise NotImplementedError(
                "Image embeddings not supported - no image_embedding_model configured"
            )
        b64 = base64.b64encode(image).decode()
        body = self._create_image_embedding_request(
            image_b64s=[b64], mime_type=mime_type
        )
        vectors = self._parse_image_embedding_response(self._invoke_image_model(body))
        self._remember_image_dim(vectors[0])
        return vectors[0]

    async def embed_image_batch(
        self, images: list[bytes], mime_type: str = "image/jpeg"
    ) -> list[list[float]]:
        if not self.supports_image_embeddings:
            raise NotImplementedError(
                "Image embeddings not supported - no image_embedding_model configured"
            )
        if not images:
            return []

        model = self.image_embedding_model or ""

        if model.startswith("cohere.embed"):
            results: list[list[float]] = []
            for i in range(0, len(images), _COHERE_IMAGE_BATCH_SIZE):
                chunk = images[i : i + _COHERE_IMAGE_BATCH_SIZE]
                b64s = [base64.b64encode(img).decode() for img in chunk]
                body = self._create_image_embedding_request(
                    image_b64s=b64s, mime_type=mime_type
                )
                vectors = self._parse_image_embedding_response(
                    self._invoke_image_model(body)
                )
                results.extend(vectors)
            self._remember_image_dim(results[0])
            return results

        # Titan and unknown models: sequential fallback
        return [await self.embed_image(img, mime_type) for img in images]

    async def embed_for_image_space(self, text: str) -> list[float]:
        if not self.supports_image_embeddings:
            raise NotImplementedError(
                "Image embeddings not supported - no image_embedding_model configured"
            )
        body = self._create_image_embedding_request(
            text=text, cohere_input_type="search_query"
        )
        vectors = self._parse_image_embedding_response(self._invoke_image_model(body))
        self._remember_image_dim(vectors[0])
        return vectors[0]

    def get_image_dimension(self) -> int:
        if not self.supports_image_embeddings:
            raise NotImplementedError(
                "Image embeddings not supported - no image_embedding_model configured"
            )
        if self._image_dimension is None:
            raise RuntimeError(
                f"Image embedding dimension not detected yet for model "
                f"{self.image_embedding_model}. Call embed_image() or "
                f"embed_for_image_space() first."
            )
        return self._image_dimension

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
                "Unknown generation model format for %s, using Claude format as default",
                self.generation_model,
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
                "Unknown generation response format for %s, trying common fields",
                self.generation_model,
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
            logger.error("Bedrock generation error: %s", e)
            raise

    async def close(self) -> None:
        """Close the client (no-op for boto3 clients)."""
        pass
