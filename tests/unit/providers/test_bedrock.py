"""Unit tests for Bedrock provider."""

import json
from unittest.mock import MagicMock

import pytest

from nextcloud_mcp_server.providers.bedrock import BOTO3_AVAILABLE, BedrockProvider


@pytest.fixture
def mock_bedrock_client(mocker):
    """Mock boto3 bedrock-runtime client."""
    if not BOTO3_AVAILABLE:
        pytest.skip("boto3 not installed")

    mock_client = MagicMock()
    mocker.patch("boto3.client", return_value=mock_client)
    return mock_client


@pytest.mark.unit
async def test_bedrock_embedding_titan(mock_bedrock_client):
    """Test Bedrock embedding with Titan model."""
    # Mock response
    mock_response = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps({"embedding": [0.1, 0.2, 0.3]}).encode()
            )
        )
    }
    mock_bedrock_client.invoke_model.return_value = mock_response

    # Create provider
    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
        generation_model=None,
    )

    # Test embedding
    embedding = await provider.embed("test text")

    assert embedding == [0.1, 0.2, 0.3]
    mock_bedrock_client.invoke_model.assert_called_once()
    call_args = mock_bedrock_client.invoke_model.call_args

    assert call_args.kwargs["modelId"] == "amazon.titan-embed-text-v2:0"
    body = json.loads(call_args.kwargs["body"])
    assert body == {"inputText": "test text"}


@pytest.mark.unit
async def test_bedrock_embedding_batch(mock_bedrock_client):
    """Test Bedrock batch embedding."""
    # Mock response
    mock_response = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps({"embedding": [0.1, 0.2, 0.3]}).encode()
            )
        )
    }
    mock_bedrock_client.invoke_model.return_value = mock_response

    # Create provider
    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
        generation_model=None,
    )

    # Test batch embedding
    embeddings = await provider.embed_batch(["text1", "text2"])

    assert len(embeddings) == 2
    assert embeddings[0] == [0.1, 0.2, 0.3]
    assert embeddings[1] == [0.1, 0.2, 0.3]
    assert mock_bedrock_client.invoke_model.call_count == 2


@pytest.mark.unit
async def test_bedrock_generation_claude(mock_bedrock_client):
    """Test Bedrock text generation with Claude model."""
    # Mock response
    mock_response = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps(
                    {"content": [{"text": "Generated response"}]}
                ).encode()
            )
        )
    }
    mock_bedrock_client.invoke_model.return_value = mock_response

    # Create provider
    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model=None,
        generation_model="anthropic.claude-3-sonnet-20240229-v1:0",
    )

    # Test generation
    text = await provider.generate("test prompt", max_tokens=100)

    assert text == "Generated response"
    mock_bedrock_client.invoke_model.assert_called_once()
    call_args = mock_bedrock_client.invoke_model.call_args

    assert call_args.kwargs["modelId"] == "anthropic.claude-3-sonnet-20240229-v1:0"
    body = json.loads(call_args.kwargs["body"])
    assert body["messages"][0]["content"] == "test prompt"
    assert body["max_tokens"] == 100


@pytest.mark.unit
async def test_bedrock_generation_llama(mock_bedrock_client):
    """Test Bedrock text generation with Llama model."""
    # Mock response
    mock_response = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps({"generation": "Llama response"}).encode()
            )
        )
    }
    mock_bedrock_client.invoke_model.return_value = mock_response

    # Create provider
    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model=None,
        generation_model="meta.llama3-8b-instruct-v1:0",
    )

    # Test generation
    text = await provider.generate("test prompt")

    assert text == "Llama response"
    body = json.loads(mock_bedrock_client.invoke_model.call_args.kwargs["body"])
    assert body["prompt"] == "test prompt"
    assert "max_gen_len" in body


@pytest.mark.unit
async def test_bedrock_both_capabilities(mock_bedrock_client):
    """Test Bedrock with both embedding and generation models."""
    # Mock responses
    embed_response = {
        "body": MagicMock(
            read=MagicMock(return_value=json.dumps({"embedding": [0.1, 0.2]}).encode())
        )
    }
    gen_response = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps({"content": [{"text": "Response"}]}).encode()
            )
        )
    }

    # Mock to return different responses based on modelId
    def mock_invoke(modelId, body, **kwargs):
        if "embed" in modelId:
            return embed_response
        else:
            return gen_response

    mock_bedrock_client.invoke_model.side_effect = mock_invoke

    # Create provider with both models
    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
        generation_model="anthropic.claude-3-sonnet-20240229-v1:0",
    )

    assert provider.supports_embeddings is True
    assert provider.supports_generation is True

    # Test both capabilities
    embedding = await provider.embed("test")
    assert embedding == [0.1, 0.2]

    text = await provider.generate("test")
    assert text == "Response"


@pytest.mark.unit
async def test_bedrock_no_embeddings():
    """Test Bedrock provider with no embedding model raises error."""
    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model=None,
        generation_model="anthropic.claude-3-sonnet-20240229-v1:0",
    )

    assert provider.supports_embeddings is False

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        await provider.embed("test")

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        await provider.embed_batch(["test"])

    with pytest.raises(NotImplementedError, match="no embedding_model configured"):
        provider.get_dimension()


@pytest.mark.unit
async def test_bedrock_no_generation():
    """Test Bedrock provider with no generation model raises error."""
    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
        generation_model=None,
    )

    assert provider.supports_generation is False

    with pytest.raises(NotImplementedError, match="no generation_model configured"):
        await provider.generate("test")


@pytest.mark.unit
async def test_bedrock_dimension_detection(mock_bedrock_client):
    """Test dimension detection for Bedrock embeddings."""
    # Mock response with specific dimension
    mock_response = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps(
                    {"embedding": [0.1] * 1536}  # 1536-dim embedding
                ).encode()
            )
        )
    }
    mock_bedrock_client.invoke_model.return_value = mock_response

    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
    )

    # Dimension not detected yet
    with pytest.raises(RuntimeError, match="not detected yet"):
        provider.get_dimension()

    # Detect dimension
    await provider._detect_dimension()

    # Now dimension should be available
    assert provider.get_dimension() == 1536


@pytest.mark.unit
async def test_bedrock_cohere_embedding(mock_bedrock_client):
    """Test Bedrock with Cohere embedding model."""
    # Mock response
    mock_response = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps({"embeddings": [[0.1, 0.2, 0.3]]}).encode()
            )
        )
    }
    mock_bedrock_client.invoke_model.return_value = mock_response

    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="cohere.embed-english-v3",
    )

    embedding = await provider.embed("test text")

    assert embedding == [0.1, 0.2, 0.3]
    body = json.loads(mock_bedrock_client.invoke_model.call_args.kwargs["body"])
    assert body == {"texts": ["test text"], "input_type": "search_document"}
