"""Unit tests for Ollama provider token-usage surfacing.

The provider has no other unit coverage; these focus on the ``*_with_usage``
methods added for usage metering (Deck #67) — provider-reported
``prompt_eval_count`` and the char-based estimate fallback when it's absent.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.providers.ollama import OllamaProvider


@pytest.fixture
def ollama_provider():
    # Construct with no models so __init__ skips _check_model_is_loaded (no
    # network call), then enable embeddings post-construction. https mock host
    # (never contacted — client.post is patched in each test).
    provider = OllamaProvider(base_url="https://ollama:11434")
    provider.embedding_model = "nomic-embed-text"
    return provider


def _embed_response(embeddings, prompt_eval_count=None):
    payload = {"embeddings": embeddings}
    if prompt_eval_count is not None:
        payload["prompt_eval_count"] = prompt_eval_count
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.unit
async def test_ollama_embed_batch_with_usage_reports_prompt_eval_count(ollama_provider):
    """prompt_eval_count from /api/embed is surfaced as the token count."""
    ollama_provider.client.post = AsyncMock(
        return_value=_embed_response([[0.1, 0.2], [0.3, 0.4]], prompt_eval_count=7)
    )

    embeddings, tokens = await ollama_provider.embed_batch_with_usage(["a", "b"])

    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert tokens == 7


@pytest.mark.unit
async def test_ollama_with_usage_estimates_when_count_absent(ollama_provider):
    """Older Ollama omits prompt_eval_count → char-based estimate."""
    ollama_provider.client.post = AsyncMock(
        return_value=_embed_response([[0.1]], prompt_eval_count=None)
    )

    _, tokens = await ollama_provider.embed_with_usage("abcdefgh")  # 8 chars → 2

    assert tokens == 2


@pytest.mark.unit
async def test_ollama_empty_batch_with_usage(ollama_provider):
    """Empty batch returns no embeddings, zero tokens, and makes no request."""
    ollama_provider.client.post = AsyncMock()

    embeddings, tokens = await ollama_provider.embed_batch_with_usage([])

    assert embeddings == []
    assert tokens == 0
    ollama_provider.client.post.assert_not_called()
