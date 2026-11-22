"""Amazon Bedrock provider for embeddings."""

import base64
import json
import logging
import re
from dataclasses import dataclass
from functools import partial
from typing import Any

import anyio

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

from .base import ImageInput, ImageInputLimits, ImageInputRejected, Provider

logger = logging.getLogger(__name__)

# The gateway caps decoded images at 5 MiB, and the worker downscales below the
# provider limit anyway, so the same conservative cap keeps both paths uniform.
_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_JSON = "application/json"


@dataclass(frozen=True)
class _ImageModelSpec:
    family: str  # "titan" | "cohere": selects the request/response shape
    dimension: int
    mrl_dims: tuple[int, ...]
    mime_types: frozenset[str]
    max_batch: int


# Known Bedrock image embedding models (ADR-042 §8). An id not listed here is
# never confirmed: Bedrock offers no model advertisement to fall back on.
_IMAGE_MODELS: dict[str, _ImageModelSpec] = {
    # Titan's inputImage carries no MIME field: only PNG and JPEG decode.
    "amazon.titan-embed-image-v1": _ImageModelSpec(
        family="titan",
        dimension=1024,
        mrl_dims=(256, 384, 1024),
        mime_types=frozenset({"image/png", "image/jpeg"}),
        max_batch=1,
    ),
    "cohere.embed-v4:0": _ImageModelSpec(
        family="cohere",
        dimension=1536,
        mrl_dims=(256, 512, 1024, 1536),
        mime_types=frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
        # Cohere takes up to 96 images per call, but one per call keeps a
        # ValidationException attributable to exactly one image (ADR-042 §5)
        # and a body far below Bedrock's request cap. Ingest embeds one image
        # per document anyway, so batching only ever helped backfills.
        # ponytail: raise with a byte budget in _chunked if backfills need it.
        max_batch=1,
    ),
}

# Cross-region inference profiles prefix the model id ("eu.cohere.embed-v4:0").
_INFERENCE_PROFILE_PREFIX = re.compile(r"^(?:us|eu|apac|global|us-gov|jp|au)\.")


def _image_model_spec(model_id: str) -> _ImageModelSpec | None:
    return _IMAGE_MODELS.get(_INFERENCE_PROFILE_PREFIX.sub("", model_id))


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
        image_embedding_model: str | None = None,
        image_embedding_dimensions: int | None = None,
    ):
        """
        Initialize Bedrock provider.

        Args:
            region_name: AWS region (e.g., "us-east-1"). Defaults to AWS_REGION env var.
            embedding_model: Model ID for embeddings (e.g., "amazon.titan-embed-text-v2:0").
                None disables embeddings.
            aws_access_key_id: AWS access key (optional, uses default credential chain if not provided)
            aws_secret_access_key: AWS secret key (optional, uses default credential chain if not provided)
            image_embedding_model: ``IMAGE_EMBEDDING_MODEL`` — a known Bedrock
                image model (Titan Multimodal, Cohere v4), optionally behind a
                cross-region inference profile. None disables image embeddings.
            image_embedding_dimensions: ``IMAGE_EMBEDDING_DIMENSIONS`` — one of
                the model's supported output widths, or None for its default.

        Raises:
            ImportError: If boto3 is not installed
        """
        if not BOTO3_AVAILABLE:
            raise ImportError(
                "boto3 is required for Bedrock provider. Install with: pip install boto3"
            )

        self.embedding_model = embedding_model
        self._dimension: int | None = None  # Detected dynamically
        self.image_embedding_model = image_embedding_model
        self._requested_image_dimensions = image_embedding_dimensions
        self._image_spec = (
            _image_model_spec(image_embedding_model) if image_embedding_model else None
        )

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
                accept=_JSON,
                contentType=_JSON,
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

    # ------------------------------------------------------------------
    # Image embeddings (ADR-042 §8)
    # ------------------------------------------------------------------

    async def confirm_image_embeddings(self) -> bool:
        """Confirm from the static model table; Bedrock advertises nothing."""
        model = self.image_embedding_model
        spec = self._image_spec
        if model is None:
            self._image_confirmed = False
        elif spec is None:
            logger.error(
                "IMAGE_EMBEDDING_MODEL=%s is not a known Bedrock image embedding "
                "model (known: %s); image embeddings stay off",
                model,
                ", ".join(sorted(_IMAGE_MODELS)),
            )
            self._image_confirmed = False
        elif (
            self._requested_image_dimensions is not None
            and self._requested_image_dimensions not in spec.mrl_dims
        ):
            logger.error(
                "IMAGE_EMBEDDING_DIMENSIONS=%s is not supported by %s "
                "(supported: %s); image embeddings stay off",
                self._requested_image_dimensions,
                model,
                ", ".join(map(str, spec.mrl_dims)),
            )
            self._image_confirmed = False
        else:
            self._image_dimension = self._requested_image_dimensions or spec.dimension
            self._image_confirmed = True
        return self._image_confirmed

    @property
    def image_input_limits(self) -> ImageInputLimits | None:
        if self._image_spec is None:
            return None
        return ImageInputLimits(
            mime_types=self._image_spec.mime_types,
            max_bytes=_IMAGE_MAX_BYTES,
            max_batch=self._image_spec.max_batch,
        )

    @property
    def _spec(self) -> _ImageModelSpec:
        if self._image_spec is None:
            raise RuntimeError(
                f"{self.image_embedding_model!r} is not a known Bedrock image model"
            )
        return self._image_spec

    def get_image_dimension(self) -> int:
        self._require_image_support()
        return self._requested_image_dimensions or self._spec.dimension

    def _image_request(
        self, *, images: list[ImageInput] | None = None, text: str | None = None
    ) -> dict[str, Any]:
        """Build the invoke_model body for the configured image model.

        Cohere v4 is asymmetric: images are documents, text is a query.
        """
        spec = self._spec
        width = self._requested_image_dimensions
        if spec.family == "titan":
            body: dict[str, Any] = {
                "embeddingConfig": {"outputEmbeddingLength": width or spec.dimension}
            }
            if images:
                body["inputImage"] = base64.b64encode(images[0].data).decode()
            if text is not None:
                body["inputText"] = text
            return body
        body = {"embedding_types": ["float"]}
        if width is not None:
            body["output_dimension"] = width
        if images:
            body["input_type"] = "search_document"
            body["images"] = [
                f"data:{img.mime_type};base64,{base64.b64encode(img.data).decode()}"
                for img in images
            ]
        if text is not None:
            body["input_type"] = "search_query"
            body["texts"] = [text]
        return body

    def _parse_image_response(self, response: dict[str, Any]) -> list[list[float]]:
        if self._spec.family == "titan":
            return [response["embedding"]]
        return response["embeddings"]["float"]

    async def _invoke_image_model(self, body: dict[str, Any]) -> list[list[float]]:
        # boto3 is synchronous, and image payloads are large: keep the event
        # loop free while the request is in flight.
        response = await anyio.to_thread.run_sync(
            partial(
                self.client.invoke_model,
                modelId=self.image_embedding_model,
                body=json.dumps(body),
                accept=_JSON,
                contentType=_JSON,
            )
        )
        return self._parse_image_response(json.loads(response["body"].read()))

    async def _embed_image_chunk(self, images: list[ImageInput]) -> list[list[float]]:
        # Every known model has max_batch=1, so a chunk is exactly one image
        # and a rejection always names it.
        try:
            return await self._invoke_image_model(self._image_request(images=images))
        except ClientError as exc:
            # ADR-042 §5: a ValidationException on a one-image request is that
            # image being refused. Throttling, access, 5xx and transport errors
            # stay retryable and propagate unchanged. The request shape itself
            # is pinned by unit tests, and confirmation has already validated
            # the width, so a config bug can't masquerade as a bad image here.
            error = exc.response.get("Error", {})
            if error.get("Code") != "ValidationException":
                raise
            message = error.get("Message", str(exc))
            logger.warning(
                "Bedrock %s rejected a %s image as invalid; skipping it: %s",
                self.image_embedding_model,
                images[0].mime_type,
                message,
            )
            raise ImageInputRejected(message, index=0) from exc

    async def embed_for_image_space(self, text: str) -> list[float]:
        self._require_image_support()
        vector = (await self._invoke_image_model(self._image_request(text=text)))[0]
        self._record_image_dimension(len(vector))
        return vector

    async def close(self) -> None:
        """Close the client (no-op for boto3 clients)."""
        pass
