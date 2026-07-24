"""Unit tests for Anthropic provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic.types import TextBlock

from nextcloud_mcp_server.providers.anthropic import AnthropicProvider


@pytest.fixture
def mock_anthropic_client(mocker):
    """Mock Anthropic AsyncAnthropic client."""
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mocker.patch(
        "nextcloud_mcp_server.providers.anthropic.AsyncAnthropic",
        return_value=mock_client,
    )
    return mock_client


@pytest.mark.unit
async def test_anthropic_generate_returns_text_block(mock_anthropic_client):
    """generate() returns the text of a leading TextBlock."""
    mock_response = MagicMock()
    mock_response.content = [TextBlock(type="text", text="hello world", citations=None)]
    mock_anthropic_client.messages.create = AsyncMock(return_value=mock_response)

    provider = AnthropicProvider(api_key="test-key")
    result = await provider.generate("prompt", max_tokens=42)

    assert result == "hello world"
    mock_anthropic_client.messages.create.assert_called_once()
    assert mock_anthropic_client.messages.create.call_args.kwargs["max_tokens"] == 42


@pytest.mark.unit
async def test_anthropic_generate_non_text_block_raises(mock_anthropic_client):
    """A non-text leading block raises a clear ValueError rather than AttributeError."""
    non_text_block = MagicMock()  # not an instance of TextBlock
    mock_response = MagicMock()
    mock_response.content = [non_text_block]
    mock_anthropic_client.messages.create = AsyncMock(return_value=mock_response)

    provider = AnthropicProvider(api_key="test-key")

    with pytest.raises(ValueError, match="Expected a text block"):
        await provider.generate("prompt")
