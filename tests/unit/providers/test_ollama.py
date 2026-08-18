"""Unit tests for Ollama provider token-usage surfacing.

The provider has no other unit coverage; these focus on the ``*_with_usage``
methods added for usage metering (Deck #67) — provider-reported
``prompt_eval_count`` and the char-based estimate fallback when it's absent.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nextcloud_mcp_server.providers.ollama import OllamaProvider, _is_transient


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

    @pytest.mark.unit
    def test_splits_on_the_character_budget(self, ollama_provider):
        ollama_provider.max_batch_chars = 100
        # Well under the 32-item cap, so only the char budget can split these.
        batches = self._batches(ollama_provider, ["x" * 40] * 5)

        assert [len(b) for b in batches] == [2, 2, 1]
        assert all(sum(len(t) for t in b) <= 100 for b in batches)

    @pytest.mark.unit
    def test_still_splits_on_the_item_cap(self, ollama_provider):
        # Ollama issue #6262: quality degrades past ~32 inputs, so the item cap
        # survives as a second bound even when the text is tiny.
        batches = self._batches(ollama_provider, ["a"] * 70)

        assert [len(b) for b in batches] == [32, 32, 6]

    @pytest.mark.unit
    def test_oversize_single_text_is_emitted_alone(self, ollama_provider):
        ollama_provider.max_batch_chars = 10
        # Must not be dropped (silent data loss) and must not loop forever.
        batches = self._batches(ollama_provider, ["a" * 50])

        assert batches == [["a" * 50]]

    @pytest.mark.unit
    def test_oversize_text_does_not_swallow_its_neighbours(self, ollama_provider):
        ollama_provider.max_batch_chars = 10
        batches = self._batches(ollama_provider, ["a" * 50, "b", "c"])

        assert batches == [["a" * 50], ["b", "c"]]

    @pytest.mark.unit
    def test_every_text_survives_the_split(self, ollama_provider):
        ollama_provider.max_batch_chars = 37
        texts = [f"chunk-{i}" * (i % 5 + 1) for i in range(50)]

        assert [t for b in self._batches(ollama_provider, texts) for t in b] == texts

    @pytest.mark.unit
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


class TestTransientRetry:
    """Ollama was the only embedding provider with no retry layer (GH #1345).

    A blip went straight to the ingest retry loop, while openai/mistral/gateway
    all rode it out. These pin the predicate; the shared backoff loop itself is
    covered by the retry helper's own tests.
    """

    def _request(self):
        return httpx.Request("POST", "https://ollama:11434/api/embed")

    def _status_error(self, code):
        request = self._request()
        return httpx.HTTPStatusError(
            f"HTTP {code}",
            request=request,
            response=httpx.Response(code, request=request),
        )

    @pytest.mark.unit
    def test_transport_errors_are_transient(self):
        assert _is_transient(httpx.ReadTimeout("", request=self._request()))
        assert _is_transient(httpx.ConnectError("refused"))
        # A model still loading into memory looks exactly like this.
        assert _is_transient(httpx.ConnectTimeout(""))

    @pytest.mark.unit
    def test_rate_limit_and_server_errors_are_transient(self):
        assert _is_transient(self._status_error(429))
        assert _is_transient(self._status_error(500))
        assert _is_transient(self._status_error(503))

    @pytest.mark.unit
    def test_permanent_client_errors_are_not_retried(self):
        # An unknown model or malformed request fails identically every attempt.
        assert not _is_transient(self._status_error(404))
        assert not _is_transient(self._status_error(400))
        assert not _is_transient(self._status_error(401))

    @pytest.mark.unit
    async def test_retries_then_succeeds(self, ollama_provider, monkeypatch):
        monkeypatch.setattr("anyio.sleep", AsyncMock())  # don't wait out the backoff
        ollama_provider.client.post = AsyncMock(
            side_effect=[
                httpx.ConnectError("ollama restarting"),
                _embed_response([[0.1, 0.2]], prompt_eval_count=3),
            ]
        )

        embeddings, tokens = await ollama_provider.embed_batch_with_usage(["a"])

        assert embeddings == [[0.1, 0.2]]
        assert tokens == 3
        assert ollama_provider.client.post.await_count == 2

    @pytest.mark.unit
    async def test_permanent_error_is_not_retried(self, ollama_provider):
        resp = MagicMock()
        resp.raise_for_status = MagicMock(side_effect=self._status_error(404))
        ollama_provider.client.post = AsyncMock(return_value=resp)

        with pytest.raises(httpx.HTTPStatusError):
            await ollama_provider.embed_batch_with_usage(["a"])

        ollama_provider.client.post.assert_awaited_once()

    @pytest.mark.unit
    async def test_timeout_logs_the_batch_shape(
        self, ollama_provider, monkeypatch, caplog
    ):
        monkeypatch.setattr("anyio.sleep", AsyncMock())
        ollama_provider.client.post = AsyncMock(
            side_effect=httpx.ReadTimeout("", request=self._request())
        )

        with caplog.at_level(logging.WARNING), pytest.raises(httpx.ReadTimeout):
            await ollama_provider.embed_batch_with_usage(["x" * 500, "y" * 500])

        # `ReadTimeout('')` alone told an operator nothing — the log must name
        # the batch shape and the knob that fixes it.
        logged = caplog.text
        assert "2 texts" in logged
        assert "1000 chars" in logged
        assert "OLLAMA_EMBED_MAX_BATCH_CHARS" in logged
