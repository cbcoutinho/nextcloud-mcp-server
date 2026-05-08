"""Unit tests for Mistral provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.providers.mistral import (
    BATCH_SIZE,
    MISTRAL_EMBEDDING_DIMENSIONS,
    MistralProvider,
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
async def test_mistral_no_embeddings_disabled():
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
