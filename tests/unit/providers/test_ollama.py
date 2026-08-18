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


class TestBatchSplitting:
    """GH #1345: one /api/embed call must stay inside the read timeout.

    Ollama embeds a batch serially, so a request's wall clock tracks its total
    text, not its item count. A fixed 32-item batch carried up to ~65k chars at
    the default chunk size and never completed on a CPU-only instance.
    """

    def _batches(self, provider, texts, batch_size=32):
        return list(provider._iter_batches(texts, batch_size))

    def test_splits_on_the_character_budget(self, ollama_provider):
        ollama_provider.max_batch_chars = 100
        # Well under the 32-item cap, so only the char budget can split these.
        batches = self._batches(ollama_provider, ["x" * 40] * 5)

        assert [len(b) for b in batches] == [2, 2, 1]
        assert all(sum(len(t) for t in b) <= 100 for b in batches)

    def test_still_splits_on_the_item_cap(self, ollama_provider):
        # Ollama issue #6262: quality degrades past ~32 inputs, so the item cap
        # survives as a second bound even when the text is tiny.
        batches = self._batches(ollama_provider, ["a"] * 70)

        assert [len(b) for b in batches] == [32, 32, 6]

    def test_oversize_single_text_is_emitted_alone(self, ollama_provider):
        ollama_provider.max_batch_chars = 10
        # Must not be dropped (silent data loss) and must not loop forever.
        batches = self._batches(ollama_provider, ["a" * 50])

        assert batches == [["a" * 50]]

    def test_oversize_text_does_not_swallow_its_neighbours(self, ollama_provider):
        ollama_provider.max_batch_chars = 10
        batches = self._batches(ollama_provider, ["a" * 50, "b", "c"])

        assert batches == [["a" * 50], ["b", "c"]]

    def test_every_text_survives_the_split(self, ollama_provider):
        ollama_provider.max_batch_chars = 37
        texts = [f"chunk-{i}" * (i % 5 + 1) for i in range(50)]

        assert [t for b in self._batches(ollama_provider, texts) for t in b] == texts

    def test_no_empty_batches(self, ollama_provider):
        ollama_provider.max_batch_chars = 5
        # Empty strings cost nothing and must not produce an empty request.
        batches = self._batches(ollama_provider, ["", "", "abcdef", ""])

        assert all(batches)

    @pytest.mark.unit
    async def test_large_document_issues_several_requests(self, ollama_provider):
        ollama_provider.max_batch_chars = 16_000
        ollama_provider.client.post = AsyncMock(
            return_value=_embed_response([[0.1]], prompt_eval_count=1)
        )
        # The reported document's shape: 326 chunks of ~1.3 KB. One 32-item
        # batch would have been ~42 KB in a single call.
        texts = ["x" * 1320] * 326

        await ollama_provider.embed_batch_with_usage(texts)

        calls = ollama_provider.client.post.await_args_list
        assert len(calls) > 11  # more than the old fixed 32-item batching
        assert all(
            sum(len(t) for t in c.kwargs["json"]["input"]) <= 16_000 + 1320
            for c in calls
        )
