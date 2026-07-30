"""Unit tests for OpenAI provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.providers.openai import (
    OPENAI_EMBEDDING_DIMENSIONS,
    OpenAIProvider,
)


@pytest.fixture
def mock_openai_client(mocker):
    """Mock OpenAI AsyncClient."""
    mock_client = MagicMock()
    mock_client.embeddings = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.close = AsyncMock()
    mocker.patch(
        "nextcloud_mcp_server.providers.openai.AsyncOpenAI", return_value=mock_client
    )
    return mock_client


@pytest.mark.unit
async def test_openai_embedding(mock_openai_client):
    """Test OpenAI embedding with text-embedding-3-small."""
    # Mock response
    mock_embedding_data = MagicMock()
    mock_embedding_data.embedding = [0.1, 0.2, 0.3]
    mock_embedding_data.index = 0

    mock_response = MagicMock()
    mock_response.data = [mock_embedding_data]

    mock_openai_client.embeddings.create = AsyncMock(return_value=mock_response)

    # Create provider
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
    )

    # Test embedding
    embedding = await provider.embed("test text")

    assert embedding == [0.1, 0.2, 0.3]
    mock_openai_client.embeddings.create.assert_called_once_with(
        input="test text",
        model="text-embedding-3-small",
    )


@pytest.mark.unit
async def test_openai_embedding_batch(mock_openai_client):
    """Test OpenAI batch embedding."""
    # Mock response
    mock_embedding_data_1 = MagicMock()
    mock_embedding_data_1.embedding = [0.1, 0.2, 0.3]
    mock_embedding_data_1.index = 0

    mock_embedding_data_2 = MagicMock()
    mock_embedding_data_2.embedding = [0.4, 0.5, 0.6]
    mock_embedding_data_2.index = 1

    mock_response = MagicMock()
    mock_response.data = [mock_embedding_data_1, mock_embedding_data_2]

    mock_openai_client.embeddings.create = AsyncMock(return_value=mock_response)

    # Create provider
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
    )

    # Test batch embedding
    embeddings = await provider.embed_batch(["text1", "text2"])

    assert len(embeddings) == 2
    assert embeddings[0] == [0.1, 0.2, 0.3]
    assert embeddings[1] == [0.4, 0.5, 0.6]
    mock_openai_client.embeddings.create.assert_called_once_with(
        input=["text1", "text2"],
        model="text-embedding-3-small",
    )


@pytest.mark.unit
async def test_openai_embedding_capability(mock_openai_client):
    """Test OpenAI advertises and serves embeddings."""
    mock_embedding_data = MagicMock()
    mock_embedding_data.embedding = [0.1, 0.2]
    mock_response = MagicMock()
    mock_response.data = [mock_embedding_data]
    mock_openai_client.embeddings.create = AsyncMock(return_value=mock_response)

    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
    )

    assert provider.supports_embeddings is True

    embedding = await provider.embed("test")
    assert embedding == [0.1, 0.2]


@pytest.mark.unit
async def test_openai_no_embeddings():
    """Test OpenAI provider with no embedding model raises error."""
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model=None,
    )

    assert provider.supports_embeddings is False

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        await provider.embed("test")

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        await provider.embed_batch(["test"])

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        provider.get_dimension()


@pytest.mark.unit
async def test_openai_known_dimension():
    """Test dimension detection for known OpenAI models."""
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
    )

    # Known model should have dimension set from lookup table
    assert provider.get_dimension() == 1536


@pytest.mark.unit
async def test_openai_unknown_dimension_detected(mock_openai_client):
    """Test dimension detection for unknown model via API call."""
    # Mock response with specific dimension
    mock_embedding_data = MagicMock()
    mock_embedding_data.embedding = [0.1] * 768
    mock_embedding_data.index = 0

    mock_response = MagicMock()
    mock_response.data = [mock_embedding_data]
    mock_openai_client.embeddings.create = AsyncMock(return_value=mock_response)

    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="custom-embedding-model",
    )

    # Dimension not known yet for custom model
    with pytest.raises(RuntimeError, match="not detected yet"):
        provider.get_dimension()

    # Detect dimension via embed call
    await provider.embed("test")

    # Now dimension should be available
    assert provider.get_dimension() == 768


@pytest.mark.unit
async def test_openai_github_models_api(mock_openai_client):
    """Test OpenAI provider with GitHub Models API configuration."""
    # Mock response
    mock_embedding_data = MagicMock()
    mock_embedding_data.embedding = [0.1, 0.2, 0.3]
    mock_embedding_data.index = 0

    mock_response = MagicMock()
    mock_response.data = [mock_embedding_data]
    mock_openai_client.embeddings.create = AsyncMock(return_value=mock_response)

    # Create provider with GitHub Models configuration
    provider = OpenAIProvider(
        api_key="ghp_test_token",
        base_url="https://models.github.ai/inference",
        embedding_model="openai/text-embedding-3-small",
    )

    # Known dimension for GitHub Models prefixed model
    assert (
        provider.get_dimension()
        == OPENAI_EMBEDDING_DIMENSIONS["openai/text-embedding-3-small"]
    )

    # Test embedding
    embedding = await provider.embed("test text")
    assert embedding == [0.1, 0.2, 0.3]


@pytest.mark.unit
async def test_openai_empty_batch():
    """Test OpenAI batch embedding with empty list."""
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
    )

    embeddings = await provider.embed_batch([])
    assert embeddings == []


def _embed_item(embedding, index):
    item = MagicMock()
    item.embedding = embedding
    item.index = index
    return item


@pytest.mark.unit
async def test_openai_embed_batch_with_usage_reports_tokens(mock_openai_client):
    """embed_batch_with_usage returns the response's total_tokens."""
    response = MagicMock()
    response.data = [_embed_item([0.1, 0.2], 0), _embed_item([0.3, 0.4], 1)]
    response.usage = MagicMock(total_tokens=9)
    mock_openai_client.embeddings.create = AsyncMock(return_value=response)

    provider = OpenAIProvider(
        api_key="test-key", embedding_model="text-embedding-3-small"
    )
    embeddings, tokens = await provider.embed_batch_with_usage(["a", "b"])

    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert tokens == 9


@pytest.mark.unit
async def test_openai_with_usage_estimates_when_usage_absent(mock_openai_client):
    """Missing usage falls back to the char-based estimate."""
    response = MagicMock()
    response.data = [_embed_item([0.1], 0)]
    response.usage = None
    mock_openai_client.embeddings.create = AsyncMock(return_value=response)

    provider = OpenAIProvider(
        api_key="test-key", embedding_model="text-embedding-3-small"
    )
    _, tokens = await provider.embed_with_usage("abcdefgh")  # 8 chars → 2 tokens

    assert tokens == 2


@pytest.mark.unit
async def test_openai_close(mock_openai_client):
    """Test OpenAI client close."""
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
    )

    await provider.close()
    mock_openai_client.close.assert_called_once()


# --- transient-error retry (card 309) ----------------------------------------


def _req():
    import httpx

    return httpx.Request("POST", "https://gw/v1/embeddings")


@pytest.mark.unit
def test_is_transient_classifies_retryable_errors():
    """Connection / timeout / 429 / 5xx are transient; 4xx and others are not."""
    import httpx
    from openai import (
        APIConnectionError,
        APITimeoutError,
        BadRequestError,
        InternalServerError,
        RateLimitError,
    )

    from nextcloud_mcp_server.providers.openai import _is_transient

    req = _req()
    assert _is_transient(APIConnectionError(request=req)) is True
    assert _is_transient(APITimeoutError(request=req)) is True
    assert (
        _is_transient(
            RateLimitError("rl", response=httpx.Response(429, request=req), body=None)
        )
        is True
    )
    assert (
        _is_transient(
            InternalServerError(
                "boom", response=httpx.Response(500, request=req), body=None
            )
        )
        is True
    )
    # Permanent client errors must NOT be retried.
    assert (
        _is_transient(
            BadRequestError("bad", response=httpx.Response(400, request=req), body=None)
        )
        is False
    )
    assert _is_transient(ValueError("unrelated")) is False


@pytest.mark.unit
async def test_embed_retries_on_connection_error(mock_openai_client, monkeypatch):
    """A transient APIConnectionError (pod rollover) is retried, not dropped."""
    from openai import APIConnectionError

    from nextcloud_mcp_server import retry as _retry

    monkeypatch.setattr(_retry.anyio, "sleep", AsyncMock(return_value=None))

    mock_embedding_data = MagicMock()
    mock_embedding_data.embedding = [0.1, 0.2, 0.3]
    mock_response = MagicMock()
    mock_response.data = [mock_embedding_data]

    # First call raises a transient connection error, second succeeds.
    create = AsyncMock(side_effect=[APIConnectionError(request=_req()), mock_response])
    mock_openai_client.embeddings.create = create
    provider = OpenAIProvider(
        api_key="test-key", embedding_model="text-embedding-3-small"
    )

    result = await provider.embed("hello")
    assert result == [0.1, 0.2, 0.3]
    assert create.await_count == 2  # one failure, one success


@pytest.mark.unit
async def test_embed_batch_retries_on_connection_error(mock_openai_client, monkeypatch):
    """The batch path (`_embed_batch_request`) shares the transient retry too."""
    from openai import APIConnectionError

    from nextcloud_mcp_server import retry as _retry

    monkeypatch.setattr(_retry.anyio, "sleep", AsyncMock(return_value=None))

    data = MagicMock()
    data.embedding = [0.4, 0.5, 0.6]
    data.index = 0
    mock_response = MagicMock()
    mock_response.data = [data]
    mock_response.usage.total_tokens = 7

    create = AsyncMock(side_effect=[APIConnectionError(request=_req()), mock_response])
    mock_openai_client.embeddings.create = create
    provider = OpenAIProvider(
        api_key="test-key", embedding_model="text-embedding-3-small"
    )

    embeddings, tokens = await provider.embed_batch_with_usage(["text"])
    assert embeddings == [[0.4, 0.5, 0.6]]
    assert tokens == 7
    assert create.await_count == 2


@pytest.mark.unit
async def test_embed_does_not_retry_on_bad_request(mock_openai_client, monkeypatch):
    """embed() fast-fails (no retry) on a permanent 4xx."""
    import httpx
    from openai import BadRequestError

    from nextcloud_mcp_server import retry as _retry

    monkeypatch.setattr(_retry.anyio, "sleep", AsyncMock(return_value=None))

    err = BadRequestError(
        "bad", response=httpx.Response(400, request=_req()), body=None
    )
    create = AsyncMock(side_effect=err)
    mock_openai_client.embeddings.create = create
    provider = OpenAIProvider(
        api_key="test-key", embedding_model="text-embedding-3-small"
    )

    with pytest.raises(BadRequestError):
        await provider.embed("hello")
    assert create.await_count == 1  # no retry on a permanent 4xx
