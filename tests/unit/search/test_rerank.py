"""Unit tests for the rerank pipeline stage.

Pool sizing lives in ``test_rerank_pool_size.py`` alongside the machinery it
belongs to; this file covers the stage's runtime behaviour.

The degradation paths carry the weight. Reranking must never fail a search, so
every failure mode has to return the input order AND report that it did — a
stage that silently returned retrieval order while claiming to have reranked
would be indistinguishable from a ranking regression.
"""

import anyio
import pytest

from nextcloud_mcp_server.providers.gateway_rerank import (
    RerankedIndex,
    RerankError,
)
from nextcloud_mcp_server.search import rerank as rerank_mod
from nextcloud_mcp_server.search.algorithms import SearchResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_state():
    """The stage caches its client/limiter/cooldown at module level."""
    rerank_mod._reset_rerank_state()
    yield
    rerank_mod._reset_rerank_state()


def _settings(**overrides):
    class _S:
        search_rerank_enabled = True
        embedding_gateway_url = "https://gw.example"
        search_rerank_model = "vendor/model"
        search_rerank_pool_size = 200
        search_rerank_timeout_seconds = 30.0
        search_rerank_max_concurrency = 1

    s = _S()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _results(n=4, *, excerpt="text"):
    return [
        SearchResult(
            id=str(i),
            doc_type="file",
            title=f"doc {i}",
            excerpt=f"{excerpt} {i}",
            score=1.0 - i / 100,
        )
        for i in range(n)
    ]


def _stub_client(monkeypatch, *, ranking=None, raises=None, capture=None):
    class _Client:
        model = "vendor/model"

        async def rerank(self, query, documents):
            if capture is not None:
                capture.append((query, list(documents)))
            if raises is not None:
                raise raises
            return ranking

    async def _get(_settings):
        return _Client()

    monkeypatch.setattr(rerank_mod, "_get_client", _get)


class TestAvailability:
    def test_unavailable_without_flag(self):
        assert not rerank_mod.rerank_available(_settings(search_rerank_enabled=False))

    def test_unavailable_without_gateway(self):
        assert not rerank_mod.rerank_available(_settings(embedding_gateway_url=""))

    def test_available_when_both_present(self):
        assert rerank_mod.rerank_available(_settings())


class TestReordering:
    async def test_reorders_and_sets_rerank_score(self, monkeypatch):
        _stub_client(
            monkeypatch,
            ranking=[
                RerankedIndex(index=2, score=0.9),
                RerankedIndex(index=0, score=0.5),
                RerankedIndex(index=1, score=0.1),
            ],
        )
        results = _results(3)

        out, reranked = await rerank_mod.rerank_results(
            results, "q", settings=_settings(), surface="mcp"
        )

        assert reranked is True
        assert [r.id for r in out] == ["2", "0", "1"]
        assert [r.rerank_score for r in out] == [0.9, 0.5, 0.1]

    async def test_retrieval_score_is_left_untouched(self, monkeypatch):
        """`score_threshold` filters on the retrieval score inside Qdrant, so
        overwriting it here would leave the filter and the returned value
        referring to different quantities."""
        _stub_client(monkeypatch, ranking=[RerankedIndex(index=1, score=0.9)])
        results = _results(2)
        original = [r.score for r in results]

        out, _ = await rerank_mod.rerank_results(
            results, "q", settings=_settings(), surface="mcp"
        )

        assert sorted(r.score for r in out) == sorted(original)

    async def test_unscored_candidates_are_appended_not_dropped(self, monkeypatch):
        """A provider that caps its results must not silently cost us recall."""
        _stub_client(monkeypatch, ranking=[RerankedIndex(index=3, score=0.9)])
        results = _results(4)

        out, reranked = await rerank_mod.rerank_results(
            results, "q", settings=_settings(), surface="mcp"
        )

        assert reranked is True
        assert len(out) == 4, "no candidate may be lost"
        assert out[0].id == "3"
        # The remainder keeps retrieval order behind the reranked head.
        assert [r.id for r in out[1:]] == ["0", "1", "2"]

    async def test_empty_excerpts_are_not_sent_but_are_kept(self, monkeypatch):
        capture: list = []
        _stub_client(
            monkeypatch, ranking=[RerankedIndex(index=0, score=0.9)], capture=capture
        )
        results = _results(3)
        results[1].excerpt = "   "

        out, reranked = await rerank_mod.rerank_results(
            results, "q", settings=_settings(), surface="mcp"
        )

        assert reranked is True
        _, sent = capture[0]
        assert len(sent) == 2, "the blank excerpt is not worth a model slot"
        assert len(out) == 3, "but the row is still returned"


class TestDegradation:
    async def test_disabled_is_a_noop(self, monkeypatch):
        results = _results(3)
        out, reranked = await rerank_mod.rerank_results(
            results,
            "q",
            settings=_settings(search_rerank_enabled=False),
            surface="mcp",
        )
        assert reranked is False
        assert [r.id for r in out] == ["0", "1", "2"]
        assert all(r.rerank_score is None for r in out)

    async def test_upstream_failure_returns_exact_retrieval_order(self, monkeypatch):
        _stub_client(monkeypatch, raises=RerankError("gateway down"))
        results = _results(4)

        out, reranked = await rerank_mod.rerank_results(
            results, "q", settings=_settings(), surface="mcp"
        )

        assert reranked is False
        assert [r.id for r in out] == ["0", "1", "2", "3"]
        assert all(r.rerank_score is None for r in out)

    async def test_failure_engages_cooldown(self, monkeypatch):
        """Without a cooldown, every search during an outage pays the full
        timeout before degrading — a component failure becomes a latency floor
        across the whole surface."""
        calls = {"n": 0}

        class _Client:
            model = "vendor/model"

            async def rerank(self, query, documents):
                calls["n"] += 1
                raise RerankError("gateway down")

        async def _get(_s):
            return _Client()

        monkeypatch.setattr(rerank_mod, "_get_client", _get)

        for _ in range(3):
            _, reranked = await rerank_mod.rerank_results(
                _results(3), "q", settings=_settings(), surface="mcp"
            )
            assert reranked is False

        assert calls["n"] == 1, "only the first search should reach the reranker"

    async def test_cooldown_expires(self, monkeypatch):
        _stub_client(monkeypatch, raises=RerankError("down"))
        await rerank_mod.rerank_results(
            _results(3), "q", settings=_settings(), surface="mcp"
        )

        # Rewind the cooldown rather than sleeping through it.
        rerank_mod._cooldown_until = anyio.current_time() - 1
        _stub_client(monkeypatch, ranking=[RerankedIndex(index=1, score=0.9)])

        _, reranked = await rerank_mod.rerank_results(
            _results(3), "q", settings=_settings(), surface="mcp"
        )
        assert reranked is True

    @pytest.mark.parametrize("n", [0, 1])
    async def test_too_few_results_to_reorder(self, monkeypatch, n):
        _stub_client(monkeypatch, ranking=[])
        out, reranked = await rerank_mod.rerank_results(
            _results(n), "q", settings=_settings(), surface="mcp"
        )
        assert reranked is False
        assert len(out) == n
