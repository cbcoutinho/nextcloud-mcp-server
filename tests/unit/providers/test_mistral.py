"""Unit tests for Mistral provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mistralai.client.errors import SDKError

from nextcloud_mcp_server.providers.mistral import (
    BATCH_SIZE,
    MISTRAL_EMBEDDING_DIMENSIONS,
    MistralProvider,
    _is_transient,
)


def _make_data(embedding: list[float], index: int) -> MagicMock:
    """Build a mock EmbeddingResponseData entry."""
    item = MagicMock()
    item.embedding = embedding
    item.index = index
    return item


def _make_response(embeddings: list[list[float]]) -> MagicMock:
    """Build a mock EmbeddingResponse with `embeddings` indexed in order."""
    response = MagicMock()
    response.data = [_make_data(emb, i) for i, emb in enumerate(embeddings)]
    return response


@pytest.fixture
def mock_mistral_client(mocker):
    """Mock the Mistral SDK constructor."""
    mock_client = MagicMock()
    mock_client.embeddings = MagicMock()
    mocker.patch(
        "nextcloud_mcp_server.providers.mistral.Mistral", return_value=mock_client
    )
    return mock_client


@pytest.mark.unit
async def test_mistral_embedding_single(mock_mistral_client):
    """Single text embed: round-trip through SDK with correct kwargs."""
    mock_mistral_client.embeddings.create_async = AsyncMock(
        return_value=_make_response([[0.1, 0.2, 0.3]])
    )

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    embedding = await provider.embed("hello world")

    assert embedding == [0.1, 0.2, 0.3]
    mock_mistral_client.embeddings.create_async.assert_awaited_once_with(
        model="mistral-embed",
        inputs=["hello world"],
    )


@pytest.mark.unit
async def test_mistral_embedding_batch_single_call(mock_mistral_client):
    """Batch smaller than BATCH_SIZE issues a single API call."""
    mock_mistral_client.embeddings.create_async = AsyncMock(
        return_value=_make_response([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]])
    )

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    embeddings = await provider.embed_batch(["a", "b", "c"])

    assert embeddings == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
    assert mock_mistral_client.embeddings.create_async.await_count == 1


@pytest.mark.unit
async def test_mistral_embedding_batch_chunking(mock_mistral_client):
    """Batches exceeding BATCH_SIZE are split into multiple API calls."""

    # Each call returns one embedding per input it received; capture by side
    # effect so we can inspect lengths per chunk.
    def _side_effect(*, model, inputs, **_kwargs):
        return _make_response([[float(i)] for i in range(len(inputs))])

    mock_mistral_client.embeddings.create_async = AsyncMock(side_effect=_side_effect)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    total = BATCH_SIZE * 2 + 5  # forces three chunks: 64, 64, 5 (with default)
    embeddings = await provider.embed_batch([f"text-{i}" for i in range(total)])

    assert len(embeddings) == total
    assert mock_mistral_client.embeddings.create_async.await_count == 3
    # Verify the chunk sizes the SDK was actually called with.
    chunk_sizes = [
        len(call.kwargs["inputs"])
        for call in mock_mistral_client.embeddings.create_async.await_args_list
    ]
    assert chunk_sizes == [BATCH_SIZE, BATCH_SIZE, 5]


@pytest.mark.unit
async def test_mistral_embedding_batch_order_preserved(mock_mistral_client):
    """Out-of-order index in response data is sorted before returning."""
    response = MagicMock()
    response.data = [
        _make_data([0.3, 0.3], 2),
        _make_data([0.1, 0.1], 0),
        _make_data([0.2, 0.2], 1),
    ]
    mock_mistral_client.embeddings.create_async = AsyncMock(return_value=response)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    embeddings = await provider.embed_batch(["x", "y", "z"])

    assert embeddings == [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]]


@pytest.mark.unit
async def test_mistral_supports_capabilities(mock_mistral_client):
    """Mistral provider advertises embeddings only."""
    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    assert provider.supports_embeddings is True
    assert provider.supports_generation is False


@pytest.mark.unit
async def test_mistral_generate_not_implemented(mock_mistral_client):
    """generate() always raises NotImplementedError."""
    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    with pytest.raises(NotImplementedError, match="does not support generation"):
        await provider.generate("test prompt")


@pytest.mark.unit
async def test_mistral_get_dimension_known_model(mock_mistral_client):
    """Known model: dimension available without an API call."""
    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    assert provider.get_dimension() == MISTRAL_EMBEDDING_DIMENSIONS["mistral-embed"]
    mock_mistral_client.embeddings.create_async.assert_not_called()


@pytest.mark.unit
async def test_mistral_get_dimension_unknown_model_detected(mock_mistral_client):
    """Unknown model: dimension detected on first embed() call."""
    mock_mistral_client.embeddings.create_async = AsyncMock(
        return_value=_make_response([[0.1] * 768])
    )

    provider = MistralProvider(api_key="test-key", embedding_model="custom-mistral")

    with pytest.raises(RuntimeError, match="not detected yet"):
        provider.get_dimension()

    await provider.embed("test")
    assert provider.get_dimension() == 768


@pytest.mark.unit
async def test_mistral_no_embeddings_disabled(mock_mistral_client):
    """Setting embedding_model=None disables the embedding capability."""
    provider = MistralProvider(api_key="test-key", embedding_model=None)
    assert provider.supports_embeddings is False

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        await provider.embed("test")
    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        await provider.embed_batch(["test"])
    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        provider.get_dimension()


@pytest.mark.unit
async def test_mistral_empty_batch(mock_mistral_client):
    """An empty batch returns [] without calling the API."""
    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    assert await provider.embed_batch([]) == []
    mock_mistral_client.embeddings.create_async.assert_not_called()


@pytest.mark.unit
async def test_mistral_close_no_error(mock_mistral_client):
    """close() is best-effort and does not raise."""
    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    # No __aexit__ on the mock by default → close() should silently no-op.
    await provider.close()


@pytest.mark.unit
async def test_mistral_base_url_passed_to_sdk(mocker):
    """base_url is forwarded as server_url to the Mistral SDK constructor."""
    mock_ctor = mocker.patch(
        "nextcloud_mcp_server.providers.mistral.Mistral", return_value=MagicMock()
    )

    MistralProvider(
        api_key="test-key",
        embedding_model="mistral-embed",
        base_url="https://example.com/mistral",
    )
    mock_ctor.assert_called_once_with(
        api_key="test-key",
        server_url="https://example.com/mistral",
    )


@pytest.mark.unit
async def test_mistral_embed_raises_on_empty_response_data(mock_mistral_client):
    """embed(): empty response.data triggers the defensive RuntimeError guard."""
    empty_response = MagicMock()
    empty_response.data = []
    mock_mistral_client.embeddings.create_async = AsyncMock(return_value=empty_response)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    with pytest.raises(RuntimeError, match="returned no embedding"):
        await provider.embed("test")


@pytest.mark.unit
async def test_mistral_embed_raises_on_null_embedding(mock_mistral_client):
    """embed(): a single response item with embedding=None is rejected."""
    null_item = MagicMock()
    null_item.embedding = None
    null_item.index = 0
    null_response = MagicMock()
    null_response.data = [null_item]
    mock_mistral_client.embeddings.create_async = AsyncMock(return_value=null_response)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    with pytest.raises(RuntimeError, match="returned no embedding"):
        await provider.embed("test")


@pytest.mark.unit
async def test_mistral_batch_raises_on_null_embedding(mock_mistral_client):
    """_embed_batch_request: a null embedding inside a batch raises explicitly."""
    good = _make_data([0.1, 0.2], 0)
    bad = MagicMock()
    bad.embedding = None
    bad.index = 1
    response = MagicMock()
    response.data = [good, bad]
    mock_mistral_client.embeddings.create_async = AsyncMock(return_value=response)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    with pytest.raises(RuntimeError, match="null embedding"):
        await provider.embed_batch(["a", "b"])


@pytest.mark.unit
async def test_mistral_batch_raises_on_count_mismatch(mock_mistral_client):
    """_embed_batch_request: fewer embeddings returned than inputs sent."""
    # Two inputs sent, one embedding returned.
    response = _make_response([[0.1, 0.2]])
    mock_mistral_client.embeddings.create_async = AsyncMock(return_value=response)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    with pytest.raises(RuntimeError, match="returned 1 embeddings for 2 inputs"):
        await provider.embed_batch(["a", "b"])


@pytest.mark.unit
async def test_mistral_embed_batch_with_usage_reports_tokens(mock_mistral_client):
    """embed_batch_with_usage returns the provider-reported total_tokens."""
    response = _make_response([[0.1, 0.2], [0.3, 0.4]])
    response.usage = MagicMock(total_tokens=11)
    mock_mistral_client.embeddings.create_async = AsyncMock(return_value=response)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    embeddings, tokens = await provider.embed_batch_with_usage(["a", "b"])

    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert tokens == 11


@pytest.mark.unit
async def test_mistral_embed_batch_with_usage_sums_across_chunks(mock_mistral_client):
    """Token counts sum across the BATCH_SIZE sub-requests (1 token/input here)."""

    def _side_effect(*, model, inputs, **_kwargs):
        resp = _make_response([[float(i)] for i in range(len(inputs))])
        resp.usage = MagicMock(total_tokens=len(inputs))
        return resp

    mock_mistral_client.embeddings.create_async = AsyncMock(side_effect=_side_effect)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    total = BATCH_SIZE * 2 + 5  # three chunks
    embeddings, tokens = await provider.embed_batch_with_usage(
        [f"t-{i}" for i in range(total)]
    )

    assert len(embeddings) == total
    assert tokens == total  # summed across all three chunks


@pytest.mark.unit
async def test_mistral_with_usage_estimates_when_usage_absent(mock_mistral_client):
    """Missing usage falls back to the char-based estimate, not a crash."""
    response = _make_response([[0.1, 0.2]])
    response.usage = None
    mock_mistral_client.embeddings.create_async = AsyncMock(return_value=response)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    _, tokens = await provider.embed_batch_with_usage(["abcd"])  # 4 chars → 1 token

    assert tokens == 1


@pytest.mark.unit
async def test_mistral_embed_with_usage_single(mock_mistral_client):
    """embed_with_usage returns the single embedding plus its token count."""
    response = _make_response([[0.5, 0.6]])
    response.usage = MagicMock(total_tokens=3)
    mock_mistral_client.embeddings.create_async = AsyncMock(return_value=response)

    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")
    embedding, tokens = await provider.embed_with_usage("hello")

    assert embedding == [0.5, 0.6]
    assert tokens == 3


@pytest.mark.unit
def test_mistral_is_transient_predicate():
    """_is_transient retries 429 (rate limit) and 5xx (server/transient) SDKErrors."""
    err_429 = MagicMock(spec=SDKError)
    err_429.status_code = 429
    err_500 = MagicMock(spec=SDKError)
    err_500.status_code = 500
    err_400 = MagicMock(spec=SDKError)
    err_400.status_code = 400

    assert _is_transient(err_429) is True
    assert _is_transient(err_500) is True  # broadened to 5xx (card 309)
    assert _is_transient(err_400) is False  # permanent client error
    # ValueError has no status_code attr → getattr returns None → False.
    assert _is_transient(ValueError()) is False


@pytest.mark.unit
async def test_mistral_embed_retries_on_5xx(mock_mistral_client, monkeypatch):
    """A 5xx SDKError is retried end-to-end (not just classified by the predicate)."""
    from nextcloud_mcp_server.providers import _retry

    monkeypatch.setattr(_retry.anyio, "sleep", AsyncMock(return_value=None))

    # Real SDKError instance (so `except SDKError` catches it) with a 5xx status.
    err = SDKError.__new__(SDKError)
    err.status_code = 500

    mock_mistral_client.embeddings.create_async = AsyncMock(
        side_effect=[err, _make_response([[0.1, 0.2, 0.3]])]
    )
    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")

    embedding = await provider.embed("hello")
    assert embedding == [0.1, 0.2, 0.3]
    assert mock_mistral_client.embeddings.create_async.await_count == 2


@pytest.mark.unit
async def test_mistral_embed_batch_retries_on_5xx(mock_mistral_client, monkeypatch):
    """The batch path (_embed_batch_request) shares the transient retry too."""
    from nextcloud_mcp_server.providers import _retry

    monkeypatch.setattr(_retry.anyio, "sleep", AsyncMock(return_value=None))

    err = SDKError.__new__(SDKError)
    err.status_code = 503

    mock_mistral_client.embeddings.create_async = AsyncMock(
        side_effect=[err, _make_response([[0.1, 0.2], [0.3, 0.4]])]
    )
    provider = MistralProvider(api_key="test-key", embedding_model="mistral-embed")

    embeddings = await provider.embed_batch(["a", "b"])
    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert mock_mistral_client.embeddings.create_async.await_count == 2
