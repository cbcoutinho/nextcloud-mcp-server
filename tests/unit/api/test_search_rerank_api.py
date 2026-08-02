"""`rerank` on POST /api/v1/search — the surface Astrolabe consumes.

Drives the real Starlette handler, mirroring test_search_granularity_api.py, so
the HTTP contract is pinned rather than the helper's internals: the default, the
capability gate, the deeper pool actually reaching the algorithm, and the
guarantee that `total_found` semantics did not shift under the deeper pool.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import unified_search
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit


def _settings(*, rerank_enabled=True):
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.search_rerank_enabled = rerank_enabled
    settings.embedding_gateway_url = "https://gw.example" if rerank_enabled else ""
    settings.search_rerank_model = "vendor/model"
    settings.search_rerank_pool_size = 200
    settings.search_rerank_timeout_seconds = 30.0
    settings.search_rerank_max_concurrency = 1
    settings.usage_metering_enabled = False
    return settings


def _app() -> Starlette:
    app = Starlette(routes=[Route("/api/v1/search", unified_search, methods=["POST"])])
    app.state.oauth_context = {"config": {"nextcloud_host": "https://nc.example"}}
    return app


def _rows(n, score=None):
    # Descending but strictly non-negative: SearchResult rejects a negative
    # score, and these fixtures go up to a full rerank pool. `score` overrides
    # with a flat value, for exercising retrieval scores on a scale that dwarfs
    # the [0, 1] rerank range (e.g. raw BM25).
    return [
        SearchResult(
            id=str(i),
            doc_type="file",
            title=f"d{i}",
            excerpt=f"t{i}",
            score=score if score is not None else 1.0 - (i / (n or 1)),
        )
        for i in range(n)
    ]


def _post(
    body,
    *,
    search_spy=None,
    rerank_enabled=True,
    rerank_impl=None,
    rows=0,
    row_score=None,
):
    algo = MagicMock()
    algo.search = search_spy or AsyncMock(return_value=_rows(rows, row_score))
    algo.query_token_count = 0
    algo.query_embedding = None

    rerank_mock = rerank_impl or AsyncMock(side_effect=lambda r, q, **kw: (r, True))

    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings",
            return_value=_settings(rerank_enabled=rerank_enabled),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.BM25HybridSearchAlgorithm",
            return_value=algo,
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.rerank_results",
            new=rerank_mock,
        ),
        # Unprovisioned caller ⇒ _search_with_acl takes the execute(None) path,
        # so the real _execute closure runs and its kwargs are observable.
        patch(
            "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
            new=AsyncMock(side_effect=NotProvisionedError("not provisioned")),
        ),
    ):
        return TestClient(_app()).post("/api/v1/search", json=body), algo, rerank_mock


def test_default_is_not_reranked():
    """Omitting the field must leave existing callers exactly as they were."""
    resp, _, rerank_mock = _post({"query": "anything"})

    assert resp.status_code == 200
    assert resp.json()["reranked"] is False
    rerank_mock.assert_not_awaited()


def test_non_boolean_rerank_is_rejected_with_400():
    resp, _, _ = _post({"query": "anything", "rerank": "yes"})

    assert resp.status_code == 400
    assert "Invalid rerank" in resp.json()["error"]


def test_rerank_on_unconfigured_server_returns_422():
    """Rejected rather than silently downgraded: a caller that asked for
    reranked ordering and got retrieval ordering cannot tell the difference
    from a ranking regression."""
    resp, _, rerank_mock = _post(
        {"query": "anything", "rerank": True}, rerank_enabled=False
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "rerank_not_configured"
    rerank_mock.assert_not_awaited()


def test_rerank_deepens_the_candidate_pool():
    """Reranking can only reorder what retrieval supplied, so the pool — not the
    caller's limit — bounds how much it can improve."""
    spy = AsyncMock(return_value=[])
    resp, _, _ = _post({"query": "q", "limit": 10, "rerank": True}, search_spy=spy)

    assert resp.status_code == 200
    assert spy.await_args.kwargs["limit"] == 200


def test_without_rerank_the_pool_is_unchanged():
    spy = AsyncMock(return_value=[])
    resp, _, _ = _post({"query": "q", "limit": 10, "offset": 5}, search_spy=spy)

    assert resp.status_code == 200
    assert spy.await_args.kwargs["limit"] == 15  # limit + offset, as before


def test_pool_never_shrinks_below_limit_plus_offset():
    """A deep-paginated request must not retrieve fewer candidates with
    reranking on than off."""
    spy = AsyncMock(return_value=[])
    resp, _, _ = _post(
        {"query": "q", "limit": 100, "offset": 900, "rerank": True}, search_spy=spy
    )

    assert resp.status_code == 200
    assert spy.await_args.kwargs["limit"] >= 1000


def test_total_found_reflects_the_returned_page_not_the_pool():
    """The deeper pool must not silently change what `total_found` means —
    Astrolabe's pager reads it, and a 10x jump would reshape the UI with no
    client change and no version signal."""
    resp, _, _ = _post({"query": "q", "limit": 5, "rerank": True}, rows=200)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 5
    assert body["total_found"] <= 5


def test_degraded_rerank_reports_false_and_still_returns_200():
    """Reranking never fails a search; the flag is how a caller tells the two
    orderings apart."""
    degraded = AsyncMock(side_effect=lambda r, q, **kw: (r, False))
    resp, _, _ = _post({"query": "q", "rerank": True}, rerank_impl=degraded, rows=3)

    assert resp.status_code == 200
    assert resp.json()["reranked"] is False


def test_rerank_score_is_exposed_when_present():
    def _with_scores(results, query, **kwargs):
        for i, r in enumerate(results):
            r.rerank_score = 0.5 + i
        return results, True

    resp, _, _ = _post(
        {"query": "q", "rerank": True},
        rerank_impl=AsyncMock(side_effect=_with_scores),
        rows=2,
    )

    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert all("rerank_score" in r for r in rows)
    # The retrieval score is still reported, so score_threshold keeps referring
    # to the same quantity a caller filters on.
    assert all("score" in r for r in rows)


def test_rerank_score_absent_when_not_reranked():
    resp, _, _ = _post({"query": "q"}, rows=2)

    assert resp.status_code == 200
    assert all("rerank_score" not in r for r in resp.json()["results"])


def test_response_echoes_granularity():
    """The response is self-describing without its originating request.

    Mirrors `SemanticSearchResponse.granularity` on the MCP surface — a stored
    or forwarded result set should say which granularity produced it, since
    chunk and document rows mean different things (`limit` counts passages vs
    documents) and are otherwise indistinguishable.
    """
    resp, _, _ = _post({"query": "q", "granularity": "document", "rerank": False})

    assert resp.status_code == 200
    assert resp.json()["granularity"] == "document"


def test_unscored_rows_stay_behind_reranked_ones_regardless_of_scale():
    """A partially-scored pool must not let raw retrieval scores outrank
    cross-encoder ones.

    `rerank_score` is calibrated in [0, 1]; `.score` is a rank artifact (~2/k
    for RRF) or an unbounded raw BM25 value. Ranking the two against each other
    lets an unscored row beat a genuinely reranked one purely by scale — a BM25
    score of 8.5 outranks every possible rerank score. `rerank_results` appends
    unscored rows in retrieval order precisely so they sit at the TAIL, and this
    handler's post-verification re-sort has to preserve that.

    Driven through the real handler on purpose: the other rerank tests use a
    uniformly scored pool, and the integration test asserts against
    `rerank_results` directly — so neither exercises this re-sort.
    """

    def _partial(results, query, **kwargs):
        # Genuinely reranked but weakly relevant; the rest stay unscored with
        # deliberately large retrieval scores.
        results[0].rerank_score = 0.01
        return results, True

    resp, _, _ = _post(
        {"query": "q", "rerank": True},
        rerank_impl=AsyncMock(side_effect=_partial),
        rows=3,
        row_score=8.5,
    )

    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert "rerank_score" in rows[0], (
        "the reranked row must lead, despite a far smaller numeric score than "
        "the unscored rows' raw retrieval scores"
    )
    assert all("rerank_score" not in r for r in rows[1:])


def test_response_granularity_defaults_to_chunk():
    resp, _, _ = _post({"query": "q"})

    assert resp.status_code == 200
    assert resp.json()["granularity"] == "chunk"


def test_total_found_is_not_halved_for_doc_types_plus_rerank():
    """The doc_types branch over-fetches 2x before verify-on-read, so its
    unreranked budget is `search_limit * 2` — not `search_limit`.

    Capping total_found flat would under-report that path by half the moment
    reranking is enabled, breaking the "reranking changes order and nothing
    else" invariant for exactly the callers who filter by type.
    """
    plain, _, _ = _post({"query": "q", "limit": 5, "doc_types": ["file"]}, rows=40)
    reranked, _, _ = _post(
        {"query": "q", "limit": 5, "doc_types": ["file"], "rerank": True}, rows=40
    )

    assert plain.status_code == 200 and reranked.status_code == 200
    assert reranked.json()["total_found"] == plain.json()["total_found"]
