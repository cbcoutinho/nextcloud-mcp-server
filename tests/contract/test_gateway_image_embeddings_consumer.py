"""Consumer contract: nextcloud-mcp-server -> embedding-gateway image embeddings.

ADR-042 step 2, gateway side astrolabe-cloud-website#1247. All interactions run
under the provider state "the gateway serves an image embedding model", which
seeds only ``bedrock/amazon.titan-embed-image-v1`` (1024-d, MRL 256/384/1024,
text and image input, PNG/JPEG).

What it pins:

- **Confirmation reads the catalogue.** ``GET /v1/models`` advertises
  ``input_modalities``, ``dimension``, ``mrl_dims`` and ``image_mime_types``;
  the client enables images only from those fields.
- **Image items on ``POST /v1/embeddings``**: ``{"type": "image", "data":
  <raw base64>, "mime_type": ...}``, floats back, at the requested width.
- **A text query into image space** is a plain string sent to the image model id.
- **The machine-readable rejection.** An image 422 is dict-shaped
  ``{"detail": {"code", "index", "message"}}``; ``unsupported_mime_type`` with
  its index is what the client turns into a permanent skip. Every other 422 is
  list-shaped and retried — that branch is unit-tested
  (tests/unit/providers/test_gateway_image_embeddings.py), since only the
  dict shape is something the gateway promises.
"""

import base64

import pytest
from pact import match
from pact.match.matcher import GenericMatcher

from nextcloud_mcp_server.providers.base import ImageInput, ImageInputRejected
from nextcloud_mcp_server.providers.gateway import GatewayProvider

pytestmark = pytest.mark.contract

_STATE = "the gateway serves an image embedding model"
_TEXT_MODEL = "mistral/mistral-embed"
_IMAGE_MODEL = "bedrock/amazon.titan-embed-image-v1"
_WIDTH = 256
# A real 1x1 PNG, so a provider that decodes the image accepts it.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
)
_WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "


def _models_interaction(pact) -> None:
    (
        pact.upon_receiving("a model listing that advertises an image model")
        .given(_STATE)
        .with_request("GET", "/v1/models")
        .will_respond_with(200)
        .with_body(
            {
                "object": match.string("list"),
                "data": match.each_like(
                    {
                        "id": match.string(_IMAGE_MODEL),
                        "object": match.string("model"),
                        "dimension": match.integer(1024),
                        "mrl_dims": match.each_like(match.integer(_WIDTH)),
                        "input_modalities": match.each_like(match.string("image")),
                        "image_mime_types": match.each_like(match.string("image/png")),
                    }
                ),
            },
            content_type="application/json",
        )
    )


def _vector_response(interaction) -> None:
    interaction.will_respond_with(200).with_body(
        {
            "object": match.string("list"),
            "data": [
                {
                    "object": match.string("embedding"),
                    "index": match.integer(0),
                    # Width is the assertion: a gateway that ignored
                    # ``dimensions`` returns 1024 floats and fails verification.
                    # The example carries the full width so the mock replays a
                    # usable vector; min/max bound it at verification.
                    "embedding": GenericMatcher(
                        "type", value=[0.0123] * _WIDTH, min=_WIDTH, max=_WIDTH
                    ),
                }
            ],
            "model": match.string(_IMAGE_MODEL),
        },
        content_type="application/json",
    )


def _provider(srv) -> GatewayProvider:
    return GatewayProvider(
        base_url=str(srv.url),
        embedding_model=_TEXT_MODEL,
        image_embedding_model=_IMAGE_MODEL,
        image_embedding_dimensions=_WIDTH,
    )


async def test_image_embedding_request(gateway_consumer_pact):
    """Confirm from the catalogue, then embed one PNG at the requested width."""
    _models_interaction(gateway_consumer_pact)
    interaction = (
        gateway_consumer_pact.upon_receiving("an image embedding request")
        .given(_STATE)
        .with_request("POST", "/v1/embeddings")
        .with_body(
            {
                "model": _IMAGE_MODEL,
                "input": [
                    {
                        "type": "image",
                        "data": base64.b64encode(_PNG).decode(),
                        "mime_type": "image/png",
                    }
                ],
                "encoding_format": "float",
                "dimensions": _WIDTH,
            },
            content_type="application/json",
        )
    )
    _vector_response(interaction)

    with gateway_consumer_pact.serve() as srv:
        provider = _provider(srv)
        try:
            assert await provider.confirm_image_embeddings() is True
            vector = await provider.embed_image(
                ImageInput(data=_PNG, mime_type="image/png")
            )
        finally:
            await provider.close()

    assert len(vector) == _WIDTH
    assert provider.get_image_dimension() == _WIDTH


async def test_text_query_into_image_space(gateway_consumer_pact):
    """A plain string sent to the image model id is embedded into image space."""
    _models_interaction(gateway_consumer_pact)
    interaction = (
        gateway_consumer_pact.upon_receiving("a text query embedded by the image model")
        .given(_STATE)
        .with_request("POST", "/v1/embeddings")
        .with_body(
            {
                "model": _IMAGE_MODEL,
                "input": ["a coastline"],
                "encoding_format": "float",
                "dimensions": _WIDTH,
            },
            content_type="application/json",
        )
    )
    _vector_response(interaction)

    with gateway_consumer_pact.serve() as srv:
        provider = _provider(srv)
        try:
            await provider.confirm_image_embeddings()
            vector = await provider.embed_for_image_space("a coastline")
        finally:
            await provider.close()

    assert len(vector) == _WIDTH


async def test_unsupported_mime_type_is_a_coded_422(gateway_consumer_pact):
    """The model rejects WebP; the 422 names the code and the image's index.

    Sent through the chunk method directly: in normal use the client's own
    pre-flight check (from ``image_mime_types``) stops a WebP before the wire.
    This pins the gateway's half of that same rule, for when the catalogue and
    the per-model check ever disagree.
    """
    (
        gateway_consumer_pact.upon_receiving(
            "an image request with a MIME type the model does not accept"
        )
        .given(_STATE)
        .with_request("POST", "/v1/embeddings")
        .with_body(
            {
                "model": _IMAGE_MODEL,
                "input": [
                    {
                        "type": "image",
                        "data": base64.b64encode(_PNG).decode(),
                        "mime_type": "image/png",
                    },
                    {
                        "type": "image",
                        "data": base64.b64encode(_WEBP).decode(),
                        "mime_type": "image/webp",
                    },
                ],
                "encoding_format": "float",
                "dimensions": _WIDTH,
            },
            content_type="application/json",
        )
        .will_respond_with(422)
        .with_body(
            {
                "detail": {
                    "code": "unsupported_mime_type",
                    "index": 1,
                    "message": match.string("image/webp is not accepted"),
                }
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        provider = _provider(srv)
        chunk = [
            ImageInput(data=_PNG, mime_type="image/png"),
            ImageInput(data=_WEBP, mime_type="image/webp"),
        ]
        try:
            with pytest.raises(ImageInputRejected) as excinfo:
                await provider._embed_image_chunk(chunk)
        finally:
            await provider.close()

    assert excinfo.value.index == 1


async def test_upstream_rejected_image_is_a_coded_422(gateway_consumer_pact):
    """An image the upstream refuses comes back as invalid_image at its index.

    Provider state: same catalogue, but the upstream refuses every image (the
    gateway maps an upstream 400 on an image-bearing call to this code instead
    of a 502). The image sits at index 1, after a text item, so the pact pins
    that ``index`` is the request position, not a count of images. This is the
    response the client turns into a permanent skip; a 502 here would loop the
    image to dead-letter.

    Sent through the request helper directly: the client never mixes text and
    images itself, but the mixed input is what makes the index meaningful.
    """
    png_item = {
        "type": "image",
        "data": base64.b64encode(_PNG).decode(),
        "mime_type": "image/png",
    }
    (
        gateway_consumer_pact.upon_receiving("an image the upstream rejects")
        .given("the upstream rejects the submitted image")
        .with_request("POST", "/v1/embeddings")
        .with_body(
            {
                "model": _IMAGE_MODEL,
                "input": ["a coastline", png_item],
                "encoding_format": "float",
                "dimensions": _WIDTH,
            },
            content_type="application/json",
        )
        .will_respond_with(422)
        .with_body(
            {
                "detail": {
                    "code": "invalid_image",
                    "index": 1,
                    "message": match.string("the upstream rejected the image"),
                }
            },
            content_type="application/json",
        )
    )

    with gateway_consumer_pact.serve() as srv:
        provider = _provider(srv)
        try:
            with pytest.raises(ImageInputRejected) as excinfo:
                await provider._embed_into_image_space(["a coastline", png_item])
        finally:
            await provider.close()

    assert excinfo.value.index == 1
