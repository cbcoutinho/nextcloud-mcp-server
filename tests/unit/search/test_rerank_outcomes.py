"""`rerank_results` distinguishes a routine skip from a real reranker failure.

Both leave the results in retrieval order, so a boolean return collapses them —
and a caller mapping that boolean onto a metric would then report every query
returning 0-1 rows as a reranker outage. Narrow filters and obscure queries do
that routinely, so the outage signal would be buried in noise exactly when it
matters.

These pin which state each path produces, since the distinction only exists to
be acted on downstream.
"""

import pytest

from nextcloud_mcp_server.providers.rerank import RerankedIndex, RerankError
from nextcloud_mcp_server.search import rerank as rerank_mod
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.rerank import (
    RERANK_APPLIED,
    RERANK_DEGRADED,
    RERANK_SKIPPED,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_state():
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


def _results(n, *, excerpt="body text"):
    return [
        SearchResult(
            id=str(i),
            doc_type="file",
            title=f"d{i}",
            excerpt=f"{excerpt} {i}" if excerpt else "",
            score=1.0 - i / 100,
        )
        for i in range(n)
    ]


def _stub(monkeypatch, *, ranking=None, raises=None):
    class _Client:
        model = "vendor/model"

        async def rerank(self, query, documents):
            if raises is not None:
                raise raises
            return ranking

    async def _get(_s):
        return _Client()

    monkeypatch.setattr(rerank_mod, "_get_client", _get)


async def test_applied_when_the_reranker_orders_the_results(monkeypatch):
    _stub(monkeypatch, ranking=[RerankedIndex(index=1, score=0.9)])

    _, outcome = await rerank_mod.rerank_results(
        _results(3), "q", settings=_settings(), surface="mcp"
    )
    assert outcome == RERANK_APPLIED


async def test_skipped_when_reranking_is_disabled(monkeypatch):
    _, outcome = await rerank_mod.rerank_results(
        _results(3),
        "q",
        settings=_settings(search_rerank_enabled=False),
        surface="mcp",
    )
    assert outcome == RERANK_SKIPPED


@pytest.mark.parametrize("n", [0, 1])
async def test_skipped_when_there_is_nothing_to_reorder(monkeypatch, n):
    """A query returning 0-1 rows is routine — a narrow filter or an obscure
    term — and must not read as a reranker failure."""
    _stub(monkeypatch, ranking=[])

    _, outcome = await rerank_mod.rerank_results(
        _results(n), "q", settings=_settings(), surface="mcp"
    )
    assert outcome == RERANK_SKIPPED


async def test_skipped_when_too_few_rows_carry_text(monkeypatch):
    """Same reasoning: unscorable rows are a property of the corpus, not of the
    reranker's health."""
    _stub(monkeypatch, ranking=[])
    results = _results(3)
    for r in results[1:]:
        r.excerpt = "   "

    _, outcome = await rerank_mod.rerank_results(
        results, "q", settings=_settings(), surface="mcp"
    )
    assert outcome == RERANK_SKIPPED


async def test_degraded_on_upstream_failure(monkeypatch):
    _stub(monkeypatch, raises=RerankError("gateway down"))

    _, outcome = await rerank_mod.rerank_results(
        _results(3), "q", settings=_settings(), surface="mcp"
    )
    assert outcome == RERANK_DEGRADED


async def test_degraded_while_in_cooldown(monkeypatch):
    """The cooldown exists only because a rerank already failed, so a search
    served from it is still the outage signal — not a routine skip."""
    _stub(monkeypatch, raises=RerankError("gateway down"))
    await rerank_mod.rerank_results(
        _results(3), "q", settings=_settings(), surface="mcp"
    )

    _, outcome = await rerank_mod.rerank_results(
        _results(3), "q", settings=_settings(), surface="mcp"
    )
    assert outcome == RERANK_DEGRADED


def test_the_three_outcomes_are_distinct():
    """Guards against a careless refactor collapsing two of them to one value,
    which would silently restore the conflation this split exists to prevent."""
    assert len({RERANK_APPLIED, RERANK_SKIPPED, RERANK_DEGRADED}) == 3
