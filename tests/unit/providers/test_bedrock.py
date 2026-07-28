"""Unit tests for Bedrock provider."""

import base64
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
        generation_model=None,
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
        generation_model=None,
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


def _mock_body(payload: dict) -> dict:
    return {
        "body": MagicMock(read=MagicMock(return_value=json.dumps(payload).encode()))
    }


@pytest.mark.unit
async def test_bedrock_image_embed_titan(mock_bedrock_client):
    """Titan multimodal: image bytes → vector via inputImage + outputEmbeddingLength."""
    mock_bedrock_client.invoke_model.return_value = _mock_body(
        {"embedding": [0.1] * 1024}
    )

    provider = BedrockProvider(
        region_name="us-east-1",
        image_embedding_model="amazon.titan-embed-image-v1",
        image_output_dim=1024,
    )
    assert provider.supports_image_embeddings is True

    vec = await provider.embed_image(b"\xff\xd8\xff\xe0fake-jpeg")

    assert len(vec) == 1024
    assert provider.get_image_dimension() == 1024
    call = mock_bedrock_client.invoke_model.call_args
    assert call.kwargs["modelId"] == "amazon.titan-embed-image-v1"
    body = json.loads(call.kwargs["body"])
    assert body["inputImage"] == base64.b64encode(b"\xff\xd8\xff\xe0fake-jpeg").decode()
    assert body["embeddingConfig"] == {"outputEmbeddingLength": 1024}
    assert "inputText" not in body


@pytest.mark.unit
async def test_bedrock_image_embed_titan_returns_error_message(mock_bedrock_client):
    """Titan returns errors via a `message` field — must surface as RuntimeError."""
    mock_bedrock_client.invoke_model.return_value = _mock_body(
        {"embedding": [], "message": "Image too small"}
    )
    provider = BedrockProvider(
        region_name="us-east-1",
        image_embedding_model="amazon.titan-embed-image-v1",
    )

    with pytest.raises(RuntimeError, match="Image too small"):
        await provider.embed_image(b"tiny")


@pytest.mark.unit
async def test_bedrock_embed_for_image_space_titan(mock_bedrock_client):
    """Text→image-space query: Titan uses inputText against the same image model."""
    mock_bedrock_client.invoke_model.return_value = _mock_body(
        {"embedding": [0.5] * 1024}
    )
    provider = BedrockProvider(
        region_name="us-east-1",
        image_embedding_model="amazon.titan-embed-image-v1",
        image_output_dim=1024,
    )

    vec = await provider.embed_for_image_space("a coast at sunset")

    assert len(vec) == 1024
    body = json.loads(mock_bedrock_client.invoke_model.call_args.kwargs["body"])
    assert body["inputText"] == "a coast at sunset"
    assert "inputImage" not in body
    assert body["embeddingConfig"] == {"outputEmbeddingLength": 1024}


@pytest.mark.unit
async def test_bedrock_image_embed_cohere_batch(mock_bedrock_client):
    """Cohere v4: batch image embedding in a single invoke_model call."""
    mock_bedrock_client.invoke_model.return_value = _mock_body(
        {"embeddings": {"float": [[0.1] * 1536, [0.2] * 1536, [0.3] * 1536]}}
    )

    provider = BedrockProvider(
        region_name="us-east-1",
        image_embedding_model="cohere.embed-v4:0",
    )

    imgs = [b"img1", b"img2", b"img3"]
    vecs = await provider.embed_image_batch(imgs, mime_type="image/png")

    assert len(vecs) == 3
    assert all(len(v) == 1536 for v in vecs)
    assert mock_bedrock_client.invoke_model.call_count == 1  # batched
    body = json.loads(mock_bedrock_client.invoke_model.call_args.kwargs["body"])
    assert body["input_type"] == "search_document"
    assert body["embedding_types"] == ["float"]
    assert len(body["images"]) == 3
    assert body["images"][0].startswith("data:image/png;base64,")


@pytest.mark.unit
async def test_bedrock_embed_for_image_space_cohere(mock_bedrock_client):
    """Cohere v4 text→image-space: input_type=search_query, single vector returned."""
    mock_bedrock_client.invoke_model.return_value = _mock_body(
        {"embeddings": {"float": [[0.7] * 1536]}}
    )
    provider = BedrockProvider(
        region_name="us-east-1",
        image_embedding_model="cohere.embed-v4:0",
    )

    vec = await provider.embed_for_image_space("hummingbird")

    assert len(vec) == 1536
    body = json.loads(mock_bedrock_client.invoke_model.call_args.kwargs["body"])
    assert body["texts"] == ["hummingbird"]
    assert body["input_type"] == "search_query"
    assert "images" not in body


@pytest.mark.unit
async def test_bedrock_image_embeddings_disabled():
    """No image_embedding_model → capability False, all image methods raise."""
    provider = BedrockProvider(
        region_name="us-east-1",
        embedding_model="amazon.titan-embed-text-v2:0",
    )
    assert provider.supports_image_embeddings is False

    with pytest.raises(NotImplementedError, match="no image_embedding_model"):
        await provider.embed_image(b"x")
    with pytest.raises(NotImplementedError, match="no image_embedding_model"):
        await provider.embed_image_batch([b"x"])
    with pytest.raises(NotImplementedError, match="no image_embedding_model"):
        await provider.embed_for_image_space("q")
    with pytest.raises(NotImplementedError, match="no image_embedding_model"):
        provider.get_image_dimension()


@pytest.mark.unit
async def test_bedrock_image_dimension_not_detected_yet():
    """get_image_dimension before any embed call raises RuntimeError."""
    provider = BedrockProvider(
        region_name="us-east-1",
        image_embedding_model="amazon.titan-embed-image-v1",
    )
    with pytest.raises(RuntimeError, match="not detected yet"):
        provider.get_image_dimension()


@pytest.mark.unit
async def test_bedrock_image_embed_cohere_chunks_over_cap(mock_bedrock_client):
    """Cohere batch >64 images chunks into multiple invoke_model calls to stay
    under the per-request cap (96)."""
    chunk1_vecs = [[0.1] * 8 for _ in range(64)]
    chunk2_vecs = [[0.2] * 8 for _ in range(36)]
    responses = [
        _mock_body({"embeddings": {"float": chunk1_vecs}}),
        _mock_body({"embeddings": {"float": chunk2_vecs}}),
    ]
    mock_bedrock_client.invoke_model.side_effect = responses

    provider = BedrockProvider(
        region_name="us-east-1",
        image_embedding_model="cohere.embed-v4:0",
    )

    images = [f"img{i}".encode() for i in range(100)]
    vecs = await provider.embed_image_batch(images)

    assert len(vecs) == 100
    assert mock_bedrock_client.invoke_model.call_count == 2
    body1 = json.loads(
        mock_bedrock_client.invoke_model.call_args_list[0].kwargs["body"]
    )
    body2 = json.loads(
        mock_bedrock_client.invoke_model.call_args_list[1].kwargs["body"]
    )
    assert len(body1["images"]) == 64
    assert len(body2["images"]) == 36


@pytest.mark.unit
async def test_bedrock_image_embed_batch_titan_sequential(mock_bedrock_client):
    """Titan has no batch endpoint — embed_image_batch falls back to sequential calls."""
    mock_bedrock_client.invoke_model.return_value = _mock_body(
        {"embedding": [0.1] * 1024}
    )
    provider = BedrockProvider(
        region_name="us-east-1",
        image_embedding_model="amazon.titan-embed-image-v1",
    )

    vecs = await provider.embed_image_batch([b"a", b"b"])

    assert len(vecs) == 2
    assert mock_bedrock_client.invoke_model.call_count == 2
