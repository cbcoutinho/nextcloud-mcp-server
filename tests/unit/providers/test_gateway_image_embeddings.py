"""GatewayProvider image embeddings (ADR-042 §8, astrolabe-cloud-website#1247).

Confirmation reads GET /v1/models; embedding posts image items to
/v1/embeddings; a dict-shaped 422 ``detail.code`` decides whether an image is
skipped permanently or the request is retried.
"""

import base64
import json

import httpx2
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.providers.base import ImageInput, ImageInputRejected
from nextcloud_mcp_server.providers.gateway import (
    _PERMANENT_IMAGE_CODES,
    GatewayProvider,
    _raise_for_image_rejection,
)

pytestmark = pytest.mark.unit

IMAGE_MODEL = "titan-embed-image-v1"
PNG = ImageInput(data=b"\x89PNG fake", mime_type="image/png")
JPEG = ImageInput(data=b"\xff\xd8 fake", mime_type="image/jpeg")

MODELS = {
    "object": "list",
    "data": [
        {"id": "mistral-embed", "object": "model", "dimension": 1024},
        {
            "id": IMAGE_MODEL,
            "object": "model",
            "dimension": 1024,
            "mrl_dims": [256, 384, 1024],
            "input_modalities": ["text", "image"],
            "image_mime_types": ["image/png", "image/jpeg"],
        },
    ],
}


def _gateway(monkeypatch, embeddings_handler=None, models=MODELS, dims=None):
    """A GatewayProvider whose httpx2 traffic hits an in-memory gateway."""
    calls: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/models"):
            if models is None:
                return httpx2.Response(503)
            return httpx2.Response(200, json=models)
        body = json.loads(request.content)
        calls.append(body)
        assert embeddings_handler is not None, "unexpected embeddings call"
        return embeddings_handler(body)

    transport = httpx2.MockTransport(handler)
    orig = httpx2.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", _client)
    provider = GatewayProvider(
        base_url="http://gw:8083",
        embedding_model="mistral-embed",
        image_embedding_model=IMAGE_MODEL,
        image_embedding_dimensions=dims,
    )
    return provider, calls


def _vectors(body: dict, width: int = 1024) -> httpx2.Response:
    # Out of order on purpose: the client must sort by ``index``.
    data = [
        {"object": "embedding", "index": i, "embedding": [float(i)] * width}
        for i in range(len(body["input"]))
    ]
    return httpx2.Response(200, json={"object": "list", "data": data[::-1]})


def _reject(code: str, index: int) -> httpx2.Response:
    return httpx2.Response(
        422, json={"detail": {"code": code, "index": index, "message": code}}
    )


async def test_confirms_from_input_modalities(monkeypatch):
    provider, _ = _gateway(monkeypatch)
    assert await provider.confirm_image_embeddings() is True
    assert provider.get_image_dimension() == 1024
    limits = provider.image_input_limits
    assert limits is not None
    assert limits.mime_types == {"image/png", "image/jpeg"}
    assert (limits.max_batch, limits.max_bytes) == (16, 5 * 1024 * 1024)


@pytest.mark.parametrize(
    "models, dims",
    [
        (None, None),  # catalogue unreachable: fail closed
        ({"data": [{"id": IMAGE_MODEL, "dimension": 1024}]}, None),  # text-only
        ({"data": []}, None),  # model not served
        (MODELS, 512),  # width not in mrl_dims
        (  # probe failed upstream: no dimension and none requested
            {"data": [{"id": IMAGE_MODEL, "input_modalities": ["image"]}]},
            None,
        ),
        (  # image-capable but no MIME types: every image would be rejected
            {
                "data": [
                    {
                        "id": IMAGE_MODEL,
                        "dimension": 1024,
                        "input_modalities": ["image"],
                    }
                ]
            },
            None,
        ),
    ],
)
async def test_not_confirmed(monkeypatch, models, dims):
    """Every doubt leaves the feature off, never on (ADR-042 §7)."""
    provider, _ = _gateway(monkeypatch, models=models, dims=dims)
    assert await provider.confirm_image_embeddings() is False
    assert provider.supports_image_embeddings is False


async def test_embeds_image_items_in_order(monkeypatch):
    """Image items go out as raw base64 with their MIME type; floats come back."""
    provider, calls = _gateway(monkeypatch, lambda b: _vectors(b, 256), dims=256)
    await provider.confirm_image_embeddings()

    vectors = await provider.embed_image_batch([PNG, JPEG])

    assert [v[0] for v in vectors] == [0.0, 1.0]
    (body,) = calls
    assert body["model"] == IMAGE_MODEL
    assert body["encoding_format"] == "float"
    assert body["dimensions"] == 256
    assert body["input"][0] == {
        "type": "image",
        "data": base64.b64encode(PNG.data).decode(),
        "mime_type": "image/png",
    }


async def test_chunks_at_sixteen(monkeypatch):
    provider, calls = _gateway(monkeypatch, _vectors)
    await provider.confirm_image_embeddings()
    assert len(await provider.embed_image_batch([PNG] * 20)) == 20
    assert [len(c["input"]) for c in calls] == [16, 4]


async def test_text_query_goes_to_the_image_model(monkeypatch):
    """A plain string sent to the image model id lands in image space."""
    provider, calls = _gateway(monkeypatch, _vectors)
    await provider.confirm_image_embeddings()
    assert len(await provider.embed_for_image_space("a coastline")) == 1024
    assert calls[0]["input"] == ["a coastline"]
    assert calls[0]["model"] == IMAGE_MODEL


@pytest.mark.parametrize("code", ["invalid_image", "unsupported_mime_type"])
async def test_permanent_codes_raise_rejected_with_rebased_index(monkeypatch, code):
    """The index is rebased from the failing chunk onto the caller's batch."""

    def handler(body):
        return _reject(code, 1) if len(body["input"]) == 4 else _vectors(body)

    provider, _ = _gateway(monkeypatch, handler)
    await provider.confirm_image_embeddings()
    with pytest.raises(ImageInputRejected) as excinfo:
        await provider.embed_image_batch([PNG] * 20)
    assert excinfo.value.index == 17


@pytest.mark.parametrize(
    "response",
    [
        _reject("image_too_large", 0),  # our downscaling is broken
        _reject("too_many_images", 0),  # our chunking is broken
        httpx2.Response(422, json={"detail": [{"loc": ["body"], "msg": "bad"}]}),
        httpx2.Response(503, json={"detail": "upstream down"}),
    ],
)
async def test_other_errors_stay_retryable(monkeypatch, response):
    """Nothing but a permanent code names the image: retry, never skip."""
    provider, _ = _gateway(monkeypatch, lambda _: response)
    await provider.confirm_image_embeddings()
    with pytest.raises(httpx2.HTTPStatusError):
        await provider.embed_image_batch([PNG])


_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=10,
)
# Hard-coded on purpose: an oracle independent of _PERMANENT_IMAGE_CODES.
_PERMANENT = ["invalid_image", "unsupported_mime_type"]


@given(
    st.sampled_from([*_PERMANENT, "image_too_large", "too_many_images"])
    | st.text(max_size=20),
    st.fixed_dictionaries(
        {},
        optional={"index": st.none() | st.integers() | st.text(), "message": st.text()},
    ),
)
@example("invalid_image", {})
@example("unsupported_mime_type", {"index": 3})
def test_only_a_permanent_code_names_the_image(code, extra):
    """A dict-shaped 422 skips the image only for a permanent code; anything
    else returns, leaving raise_for_status to make it retryable."""
    resp = httpx2.Response(422, json={"detail": {"code": code, **extra}})
    if code not in _PERMANENT:
        assert _raise_for_image_rejection(resp) is None
        return
    with pytest.raises(ImageInputRejected) as excinfo:
        _raise_for_image_rejection(resp)
    index = extra.get("index")
    assert excinfo.value.index == (index if isinstance(index, int) else None)


# Arbitrary JSON reaches the object branch on few draws, so object bodies with
# a dict detail are mixed in.
_object_bodies = st.fixed_dictionaries(
    {"detail": st.none() | st.text() | st.integers() | st.lists(_json)}
) | st.fixed_dictionaries(
    {
        "detail": st.fixed_dictionaries(
            {"code": st.text() | st.sampled_from(sorted(_PERMANENT_IMAGE_CODES))},
            optional={"index": _json, "message": _json},
        )
    }
)


@given(_json | _object_bodies)
@example([])
@example("text")
@example({"detail": {"code": []}})  # unhashable: once a TypeError on the set lookup
def test_rejection_parser_raises_only_image_input_rejected(body):
    try:
        # json.dumps, not json=: httpx2 refuses NaN, which a real body may carry.
        _raise_for_image_rejection(httpx2.Response(422, content=json.dumps(body)))
    except ImageInputRejected:
        return


@pytest.mark.parametrize("path", ["image", "text"])
async def test_width_differing_from_the_advertisement_is_fatal(monkeypatch, path):
    """No width requested: the advertised width is expected, not learned.

    A gateway whose default differs from its catalogue entry would otherwise
    define the width on the first vector while the collection is sized from
    get_image_dimension(), failing only at upsert.
    """
    provider, _ = _gateway(monkeypatch, lambda b: _vectors(b, 512))
    await provider.confirm_image_embeddings()
    assert provider.get_image_dimension() == 1024
    call = (
        provider.embed_image_batch([PNG])
        if path == "image"
        else provider.embed_for_image_space("a coastline")
    )
    with pytest.raises(RuntimeError, match="does not match"):
        await call


async def test_a_dropped_item_fails_instead_of_misaligning(monkeypatch):
    """Two images in, one vector back: never pair vectors with the wrong image."""
    one_vector = {
        "data": [{"object": "embedding", "index": 0, "embedding": [0.0] * 1024}]
    }
    provider, _ = _gateway(monkeypatch, lambda _: httpx2.Response(200, json=one_vector))
    await provider.confirm_image_embeddings()
    with pytest.raises(RuntimeError, match="indices"):
        await provider.embed_image_batch([PNG, JPEG])
