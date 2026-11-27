"""Unit tests for the provider-agnostic image embedding capability (ADR-042 §8)."""

import base64
import json
import math
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from nextcloud_mcp_server.providers.base import (
    ImageInput,
    ImageInputLimits,
    ImageInputRejected,
    Provider,
)
from nextcloud_mcp_server.providers.bedrock import BOTO3_AVAILABLE, BedrockProvider
from nextcloud_mcp_server.providers.simple import SIMPLE_IMAGE_MODEL, SimpleProvider

pytestmark = pytest.mark.unit

PNG = ImageInput(data=b"\x89PNG fake", mime_type="image/png")
JPEG = ImageInput(data=b"\xff\xd8 fake", mime_type="image/jpeg")
WEBP = ImageInput(data=b"RIFF fake", mime_type="image/webp")


class _TextOnlyProvider(Provider):
    """A provider that implements only the abstract text surface."""

    @property
    def supports_embeddings(self) -> bool:
        return True

    async def embed(self, text: str) -> list[float]:
        return [0.0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def get_dimension(self) -> int:
        return 1

    async def close(self) -> None:
        pass


# --- base class defaults --------------------------------------------------


async def test_text_only_provider_reports_no_image_support():
    """A provider that adds nothing stays unsupported, with no code of its own."""
    provider = _TextOnlyProvider()
    assert await provider.confirm_image_embeddings() is False
    assert provider.supports_image_embeddings is False
    assert provider.image_input_limits is None
    with pytest.raises(NotImplementedError):
        provider.get_image_dimension()
    with pytest.raises(NotImplementedError):
        await provider.embed_for_image_space("a coastline")


async def test_embed_image_batch_requires_confirmation():
    """Configured but unconfirmed is not enabled (ADR-042 §7)."""
    provider = SimpleProvider(image_embedding_model=SIMPLE_IMAGE_MODEL)
    with pytest.raises(RuntimeError, match="not enabled"):
        await provider.embed_image_batch([PNG])


# --- SimpleProvider -------------------------------------------------------


@given(st.none() | st.text().filter(lambda m: m != SIMPLE_IMAGE_MODEL))
@example("")
@example("amazon.titan-embed-image-v1")
async def test_simple_confirms_only_the_sentinel(model):
    """The autodetect fallback must never confirm a real-looking model id."""
    provider = SimpleProvider(image_embedding_model=model)
    assert await provider.confirm_image_embeddings() is False
    assert provider.supports_image_embeddings is False


async def test_simple_sentinel_is_confirmed_at_text_width():
    provider = SimpleProvider(dimension=32, image_embedding_model=SIMPLE_IMAGE_MODEL)
    assert await provider.confirm_image_embeddings() is True
    assert provider.get_image_dimension() == 32
    assert len(await provider.embed_for_image_space("a coastline")) == 32


@given(data=st.binary(max_size=256))
async def test_simple_image_vectors_are_deterministic_unit_vectors(data):
    """Any bytes → the same unit vector every time (hermetic e2e relies on it)."""
    provider = SimpleProvider(dimension=16, image_embedding_model=SIMPLE_IMAGE_MODEL)
    await provider.confirm_image_embeddings()
    image = ImageInput(data=data, mime_type="image/png")
    first, second = await provider.embed_image_batch([image, image])
    assert first == second
    assert math.sqrt(sum(x * x for x in first)) == pytest.approx(1.0)


class _ChunkedProvider(SimpleProvider):
    """SimpleProvider at max_batch=2, scripted per chunk, recording calls."""

    def __init__(self, reject_at: int | None = None, width: int = 16):
        super().__init__(dimension=16, image_embedding_model=SIMPLE_IMAGE_MODEL)
        self.reject_at, self.width, self.chunks = reject_at, width, []

    @property
    def image_input_limits(self) -> ImageInputLimits:
        return ImageInputLimits(mime_types=frozenset({"image/png"}), max_batch=2)

    async def _embed_image_chunk(self, images):
        self.chunks.append(len(images))
        if self.reject_at is not None and len(self.chunks) - 1 == self.reject_at:
            raise ImageInputRejected("bad", index=1)
        return [[0.0] * self.width for _ in images]


async def test_rejected_index_is_rebased_onto_the_callers_batch():
    """Chunk 2 rejects its 2nd image (chunk-relative 1) → caller's index 3."""
    provider = _ChunkedProvider(reject_at=1)
    await provider.confirm_image_embeddings()
    with pytest.raises(ImageInputRejected) as excinfo:
        await provider.embed_image_batch([PNG] * 5)
    assert excinfo.value.index == 3


async def test_a_short_chunk_fails_instead_of_misaligning():
    """One vector for two images must fail, not shorten the result."""
    provider = _ChunkedProvider()
    await provider.confirm_image_embeddings()
    provider._embed_image_chunk = lambda images: _one_vector()  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="returned 1 vectors for 2 images"):
        await provider.embed_image_batch([PNG, PNG])


async def _one_vector() -> list[list[float]]:
    return [[0.0] * 16]


async def test_wrong_width_fails_on_the_first_chunk():
    """A wrong-width model stops before the remaining chunks are sent."""
    provider = _ChunkedProvider(width=8)
    await provider.confirm_image_embeddings()
    provider._requested_image_dimensions = 16
    with pytest.raises(RuntimeError, match="does not match"):
        await provider.embed_image_batch([PNG] * 6)
    assert provider.chunks == [2]


async def test_unsupported_mime_type_is_rejected_before_any_request():
    """A type the model doesn't take is a permanent skip, with its batch index."""
    provider = SimpleProvider(image_embedding_model=SIMPLE_IMAGE_MODEL)
    await provider.confirm_image_embeddings()
    heic = ImageInput(data=b"heic", mime_type="image/heic")
    with pytest.raises(ImageInputRejected) as excinfo:
        await provider.embed_image_batch([PNG, heic])
    assert excinfo.value.index == 1


# --- Bedrock --------------------------------------------------------------


@pytest.fixture
def bedrock_client(mocker):
    if not BOTO3_AVAILABLE:
        pytest.skip("boto3 not installed")
    client = MagicMock()
    mocker.patch("boto3.client", return_value=client)
    return client


def _respond(client: MagicMock, *bodies: dict) -> None:
    client.invoke_model.side_effect = [
        {"body": MagicMock(read=MagicMock(return_value=json.dumps(b).encode()))}
        for b in bodies
    ]


def _sent_bodies(client: MagicMock) -> list[dict]:
    return [json.loads(c.kwargs["body"]) for c in client.invoke_model.call_args_list]


def _client_error(code: str, message: str = "boom"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "InvokeModel")


async def _confirmed(model: str, dims: int | None = None) -> BedrockProvider:
    provider = BedrockProvider(
        region_name="eu-west-1",
        image_embedding_model=model,
        image_embedding_dimensions=dims,
    )
    assert await provider.confirm_image_embeddings() is True
    return provider


@pytest.mark.parametrize(
    "model, dims, confirmed",
    [
        ("amazon.titan-embed-image-v1", None, True),
        ("amazon.titan-embed-image-v1", 384, True),
        ("amazon.titan-embed-image-v1", 512, False),  # not a Titan width
        ("cohere.embed-v4:0", 1024, True),
        ("eu.cohere.embed-v4:0", None, True),  # cross-region inference profile
        ("amazon.titan-embed-text-v2:0", None, False),  # text-only model
        (None, None, False),
    ],
)
async def test_bedrock_confirms_from_the_model_table(
    bedrock_client, model, dims, confirmed
):
    provider = BedrockProvider(
        region_name="eu-west-1",
        image_embedding_model=model,
        image_embedding_dimensions=dims,
    )
    assert await provider.confirm_image_embeddings() is confirmed


async def test_bedrock_titan_sends_one_image_per_call(bedrock_client):
    """Titan takes a single image per request; the base class chunks to 1."""
    provider = await _confirmed("amazon.titan-embed-image-v1", dims=256)
    _respond(bedrock_client, {"embedding": [0.1] * 256}, {"embedding": [0.2] * 256})

    vectors = await provider.embed_image_batch([PNG, JPEG])

    assert [v[0] for v in vectors] == [0.1, 0.2]
    first, second = _sent_bodies(bedrock_client)
    assert first == {
        "embeddingConfig": {"outputEmbeddingLength": 256},
        "inputImage": base64.b64encode(PNG.data).decode(),
    }
    assert second["inputImage"] == base64.b64encode(JPEG.data).decode()


async def test_bedrock_titan_rejects_webp_without_a_request(bedrock_client):
    """Titan's inputImage has no MIME field: WebP is a permanent skip, pre-flight."""
    provider = await _confirmed("amazon.titan-embed-image-v1")
    with pytest.raises(ImageInputRejected):
        await provider.embed_image_batch([WEBP])
    bedrock_client.invoke_model.assert_not_called()


async def test_bedrock_cohere_sends_one_image_per_call(bedrock_client):
    """Cohere images go as data URIs typed search_document, one per call.

    One per call keeps a ValidationException attributable to a single image
    and the body far below Bedrock's request cap (ADR-042 §5).
    """
    provider = await _confirmed("cohere.embed-v4:0", dims=256)
    _respond(
        bedrock_client,
        {"embeddings": {"float": [[0.1] * 256]}},
        {"embeddings": {"float": [[0.2] * 256]}},
    )

    vectors = await provider.embed_image_batch([PNG, WEBP])

    assert [v[0] for v in vectors] == [0.1, 0.2]
    first, second = _sent_bodies(bedrock_client)
    assert first["input_type"] == "search_document"
    assert first["output_dimension"] == 256
    assert first["images"] == [
        f"data:image/png;base64,{base64.b64encode(PNG.data).decode()}"
    ]
    assert second["images"] == [
        f"data:image/webp;base64,{base64.b64encode(WEBP.data).decode()}"
    ]


async def test_bedrock_cohere_text_query_is_search_query(bedrock_client):
    """Cohere v4 is asymmetric: a text query into image space is search_query."""
    provider = await _confirmed("cohere.embed-v4:0")
    _respond(bedrock_client, {"embeddings": {"float": [[0.3] * 1536]}})

    vector = await provider.embed_for_image_space("a coastline")

    assert len(vector) == 1536
    (body,) = _sent_bodies(bedrock_client)
    assert body["input_type"] == "search_query"
    assert body["texts"] == ["a coastline"]
    assert "images" not in body


# Classification table (ADR-042 §5): only ValidationException is permanent.
@pytest.mark.parametrize("model", ["amazon.titan-embed-image-v1", "cohere.embed-v4:0"])
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.just("ValidationException")
    | st.sampled_from(
        [
            "ThrottlingException",
            "ServiceUnavailableException",
            "InternalServerException",
            "ModelTimeoutException",
            "AccessDeniedException",
            # Case matters: a near-miss of the one permanent code is retryable.
            "validationexception",
        ]
    )
    | st.text(max_size=30)
)
# The permanent side is a single input, which generation draws at most once.
@example("ValidationException")
async def test_bedrock_error_classification(bedrock_client, model, code):
    """The second image fails; a rejection names it, anything else propagates.

    The fixture is safe to share across examples: every one resets
    ``invoke_model.side_effect`` before calling.
    """
    permanent = code == "ValidationException"
    event(f"permanent: {permanent}")
    provider = await _confirmed(model)
    ok = (
        {"embedding": [0.0] * 1024}
        if model.startswith("amazon")
        else {"embeddings": {"float": [[0.0] * 1536]}}
    )
    _respond(bedrock_client, ok)
    success = next(bedrock_client.invoke_model.side_effect)
    bedrock_client.invoke_model.side_effect = [success, _client_error(code)]

    expected = ImageInputRejected if permanent else type(_client_error(code))
    with pytest.raises(expected) as excinfo:
        await provider.embed_image_batch([JPEG, PNG])
    if permanent:
        # One image per call, so the rejected image is always identified.
        assert excinfo.value.index == 1


@pytest.mark.parametrize(
    "message, permanent",
    [
        ("Unable to process the provided image", True),
        ("The provided model identifier is invalid.", False),
        (
            "Invocation of model ID cohere.embed-v4:0 with on-demand throughput "
            "isn't supported. Retry your request with the ID or ARN of an "
            "inference profile that contains this model.",
            False,
        ),
    ],
)
async def test_bedrock_model_level_validation_stays_retryable(
    bedrock_client, message, permanent
):
    """A ValidationException about the model is config, not a bad image.

    Skipping is permanent, so a wrong model id or a missing inference profile
    must stay retryable, or every image would be marked skipped.
    """
    from botocore.exceptions import ClientError

    provider = await _confirmed("cohere.embed-v4:0")
    bedrock_client.invoke_model.side_effect = _client_error(
        "ValidationException", message
    )
    expected = ImageInputRejected if permanent else ClientError
    with pytest.raises(expected):
        await provider.embed_image_batch([PNG])


async def test_bedrock_invokes_with_the_inference_profile_id(bedrock_client):
    """The table lookup strips the profile prefix; the call must not."""
    provider = await _confirmed("eu.cohere.embed-v4:0")
    _respond(bedrock_client, {"embeddings": {"float": [[0.0] * 1536]}})
    await provider.embed_image_batch([PNG])
    assert bedrock_client.invoke_model.call_args.kwargs["modelId"] == (
        "eu.cohere.embed-v4:0"
    )


async def test_bedrock_width_mismatch_is_fatal(bedrock_client):
    """A width other than the requested one must never reach the collection."""
    provider = await _confirmed("amazon.titan-embed-image-v1", dims=384)
    _respond(bedrock_client, {"embedding": [0.0] * 1024})
    with pytest.raises(RuntimeError, match="does not match"):
        await provider.embed_image_batch([PNG])


async def test_bedrock_unrequested_width_is_checked_against_the_table(bedrock_client):
    """With no width requested, the table's width is expected, not learned.

    Otherwise the first vector would define the width while the collection is
    sized from get_image_dimension(), and the mismatch would only surface at
    upsert.
    """
    provider = await _confirmed("amazon.titan-embed-image-v1")
    assert provider.get_image_dimension() == 1024
    _respond(bedrock_client, {"embedding": [0.0] * 384})
    with pytest.raises(RuntimeError, match="does not match"):
        await provider.embed_image_batch([PNG])


def test_image_input_limits_rejects_a_zero_batch():
    with pytest.raises(ValueError, match="max_batch"):
        ImageInputLimits(mime_types=frozenset({"image/png"}), max_batch=0)


async def test_oversized_image_is_a_caller_bug_not_a_rejection(bedrock_client):
    """Over max_bytes means the caller skipped downscaling: ValueError, no skip."""
    provider = await _confirmed("amazon.titan-embed-image-v1")
    huge = ImageInput(data=b"\0" * (5 * 1024 * 1024 + 1), mime_type="image/png")
    with pytest.raises(ValueError, match="downscale"):
        await provider.embed_image_batch([huge])
    bedrock_client.invoke_model.assert_not_called()
