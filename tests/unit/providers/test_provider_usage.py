"""Token-usage surfacing: the Provider ABC estimate default + SimpleProvider.

The usage-metering hooks (Deck #67) bill ``tokens_embedded`` by tokens. Real
providers report exact counts from their API response; providers without a token
field (Simple, and the ABC default) fall back to a char-based estimate so the
billable value stays non-zero and monotone with input size.
"""

import pytest

from nextcloud_mcp_server.providers.base import Provider
from nextcloud_mcp_server.providers.simple import SimpleProvider


@pytest.mark.unit
def test_estimate_tokens_is_char_based():
    """~4-chars-per-token, ceil-rounded, summed across inputs."""
    assert Provider._estimate_tokens(["abcd"]) == 1  # 4 chars
    assert Provider._estimate_tokens(["abcde"]) == 2  # 5 chars → ceil(5/4)
    assert Provider._estimate_tokens(["ab", "cd"]) == 1  # 4 chars total
    assert Provider._estimate_tokens([]) == 0
    assert Provider._estimate_tokens([""]) == 0


@pytest.mark.unit
async def test_simple_provider_embed_with_usage_estimates():
    """SimpleProvider has no real usage → estimate path via the ABC default."""
    provider = SimpleProvider(dimension=8)
    embedding, tokens = await provider.embed_with_usage("abcdefgh")  # 8 chars → 2

    assert len(embedding) == 8
    assert tokens == 2


@pytest.mark.unit
async def test_simple_provider_embed_batch_with_usage_estimates():
    """Batch estimate sums character counts across all inputs."""
    provider = SimpleProvider(dimension=8)
    embeddings, tokens = await provider.embed_batch_with_usage(["abcd", "efgh"])

    assert len(embeddings) == 2
    assert tokens == 2  # 8 chars total → 2 tokens


@pytest.mark.unit
async def test_simple_provider_empty_batch_with_usage():
    """Empty batch returns no embeddings and zero tokens."""
    provider = SimpleProvider(dimension=8)
    embeddings, tokens = await provider.embed_batch_with_usage([])

    assert embeddings == []
    assert tokens == 0
