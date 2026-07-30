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
    )

    # Test batch embedding
    embeddings = await provider.embed_batch(["text1", "text2"])

    assert len(embeddings) == 2
    assert embeddings[0] == [0.1, 0.2, 0.3]
    assert embeddings[1] == [0.1, 0.2, 0.3]
    assert mock_bedrock_client.invoke_model.call_count == 2


@pytest.mark.unit
async def test_bedrock_embedding_capability(mock_bedrock_client):
    """Test Bedrock advertises and serves embeddings."""
    mock_bedrock_client.invoke_model.return_value = {
        "body": MagicMock(
            read=MagicMock(return_value=json.dumps({"embedding": [0.1, 0.2]}).encode())
        )
    }

    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
    )

    assert provider.supports_embeddings is True

    embedding = await provider.embed("test")
    assert embedding == [0.1, 0.2]


@pytest.mark.unit
async def test_bedrock_no_embeddings():
    """Test Bedrock provider with no embedding model raises error."""
    provider = BedrockProvider(
        region_name="us-east-1",
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


def _titan_body(embedding, token_count=None):
    payload = {"embedding": embedding}
    if token_count is not None:
        payload["inputTextTokenCount"] = token_count
    return {
        "body": MagicMock(read=MagicMock(return_value=json.dumps(payload).encode()))
    }


@pytest.mark.unit
async def test_bedrock_embed_with_usage_reports_titan_tokens(mock_bedrock_client):
    """Titan's inputTextTokenCount is surfaced as the token count."""
    mock_bedrock_client.invoke_model.return_value = _titan_body(
        [0.1, 0.2], token_count=6
    )

    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
    )
    embedding, tokens = await provider.embed_with_usage("test text")

    assert embedding == [0.1, 0.2]
    assert tokens == 6


@pytest.mark.unit
async def test_bedrock_embed_batch_with_usage_sums_token_counts(mock_bedrock_client):
    """Sequential per-text calls sum their inputTextTokenCount values."""
    mock_bedrock_client.invoke_model.return_value = _titan_body(
        [0.1, 0.2], token_count=4
    )

    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
    )
    embeddings, tokens = await provider.embed_batch_with_usage(["t1", "t2", "t3"])

    assert len(embeddings) == 3
    assert tokens == 12  # 4 tokens per call × 3 calls


@pytest.mark.unit
async def test_bedrock_with_usage_estimates_when_token_count_absent(
    mock_bedrock_client,
):
    """Cohere returns no inputTextTokenCount → char-based estimate."""
    mock_bedrock_client.invoke_model.return_value = {
        "body": MagicMock(
            read=MagicMock(
                return_value=json.dumps({"embeddings": [[0.1, 0.2]]}).encode()
            )
        )
    }

    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="cohere.embed-english-v3",
    )
    _, tokens = await provider.embed_with_usage("abcdefgh")  # 8 chars → 2 tokens

    assert tokens == 2


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
