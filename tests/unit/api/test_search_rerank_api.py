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


def _rows(n):
    # Descending but strictly non-negative: SearchResult rejects a negative
    # score, and these fixtures go up to a full rerank pool.
    return [
        SearchResult(
            id=str(i),
            doc_type="file",
            title=f"d{i}",
            excerpt=f"t{i}",
            score=1.0 - (i / (n or 1)),
        )
        for i in range(n)
    ]


def _post(body, *, search_spy=None, rerank_enabled=True, rerank_impl=None, rows=0):
    algo = MagicMock()
    algo.search = search_spy or AsyncMock(return_value=_rows(rows))
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
