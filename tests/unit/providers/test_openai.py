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
        generation_model=None,
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
        generation_model=None,
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
async def test_openai_generation(mock_openai_client):
    """Test OpenAI text generation."""
    # Mock response
    mock_choice = MagicMock()
    mock_choice.message.content = "Generated response"

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    mock_openai_client.chat.completions.create = AsyncMock(return_value=mock_response)

    # Create provider
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model=None,
        generation_model="gpt-4o-mini",
    )

    # Test generation
    text = await provider.generate("test prompt", max_tokens=100)

    assert text == "Generated response"
    mock_openai_client.chat.completions.create.assert_called_once_with(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "test prompt"}],
        max_tokens=100,
        temperature=0.7,
    )


@pytest.mark.unit
async def test_openai_both_capabilities(mock_openai_client):
    """Test OpenAI with both embedding and generation models."""
    # Mock embedding response
    mock_embedding_data = MagicMock()
    mock_embedding_data.embedding = [0.1, 0.2]
    mock_embedding_data.index = 0

    mock_embed_response = MagicMock()
    mock_embed_response.data = [mock_embedding_data]
    mock_openai_client.embeddings.create = AsyncMock(return_value=mock_embed_response)

    # Mock generation response
    mock_choice = MagicMock()
    mock_choice.message.content = "Response"

    mock_gen_response = MagicMock()
    mock_gen_response.choices = [mock_choice]
    mock_openai_client.chat.completions.create = AsyncMock(
        return_value=mock_gen_response
    )

    # Create provider with both models
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
        generation_model="gpt-4o-mini",
    )

    assert provider.supports_embeddings is True
    assert provider.supports_generation is True

    # Test both capabilities
    embedding = await provider.embed("test")
    assert embedding == [0.1, 0.2]

    text = await provider.generate("test")
    assert text == "Response"


@pytest.mark.unit
async def test_openai_no_embeddings():
    """Test OpenAI provider with no embedding model raises error."""
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model=None,
        generation_model="gpt-4o-mini",
    )

    assert provider.supports_embeddings is False

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        await provider.embed("test")

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        await provider.embed_batch(["test"])

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        provider.get_dimension()


@pytest.mark.unit
async def test_openai_no_generation():
    """Test OpenAI provider with no generation model raises error."""
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
        generation_model=None,
    )

    assert provider.supports_generation is False

    with pytest.raises(NotImplementedError, match="no generation_model configured"):
        await provider.generate("test")


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
        generation_model=None,
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


@pytest.mark.unit
async def test_openai_close(mock_openai_client):
    """Test OpenAI client close."""
    provider = OpenAIProvider(
        api_key="test-key",
        embedding_model="text-embedding-3-small",
    )

    await provider.close()
    mock_openai_client.close.assert_called_once()
