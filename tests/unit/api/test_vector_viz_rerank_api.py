"""`rerank` on POST /api/v1/vector-viz/search — the surface Astrolabe's search
page consumes.

Sibling of test_search_rerank_api.py, which pins the same flag on
`/api/v1/search`. Both exist because the two handlers are separate code paths
that must agree: the Nextcloud Unified Search provider calls one and the
Astrolabe app page calls the other, so a flag honoured on only one of them is
invisible on whichever surface the user is actually looking at.

The contract this endpoint adds over its sibling is the **trim**. It has no
pagination and feeds `all_results` straight into both the response and the PCA
plot, so a deep rerank pool that is not trimmed back would return 200 rows and
200 plotted points to a caller that asked for 10.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import vector_search
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.rerank import RERANK_APPLIED, RERANK_DEGRADED
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
    app = Starlette(
        routes=[Route("/api/v1/vector-viz/search", vector_search, methods=["POST"])]
    )
    app.state.oauth_context = {"config": {"nextcloud_host": "https://nc.example"}}
    return app


def _rows(n):
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

    rerank_mock = rerank_impl or AsyncMock(
        side_effect=lambda r, q, **kw: (r, RERANK_APPLIED)
    )

    # PCA is off in every case here: it is orthogonal to the rerank contract and
    # would drag a provider/embedding stub into each test. The one thing that
    # DOES matter about it — that it is never handed the deep pool — is asserted
    # via the returned row count, which is the same list PCA plots.
    body = {"include_pca": False, **body}

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
        client = TestClient(_app())
        return client.post("/api/v1/vector-viz/search", json=body), algo, rerank_mock


def test_default_is_not_reranked():
    """Omitting the field must leave existing callers exactly as they were —
    this endpoint predates reranking and Astrolabe 0.39.5 sends no flag."""
    resp, _, rerank_mock = _post({"query": "anything"})

    assert resp.status_code == 200
    assert resp.json()["reranked"] is False
    rerank_mock.assert_not_awaited()


def test_non_boolean_rerank_is_rejected_with_400():
    resp, _, _ = _post({"query": "anything", "rerank": "yes"})

    assert resp.status_code == 400
    assert "Invalid rerank" in resp.json()["error"]


def test_rerank_on_unconfigured_server_returns_422():
    """Same capability gate as /api/v1/search: rejected rather than silently
    downgraded, so a caller cannot mistake retrieval order for reranked order."""
    resp, _, rerank_mock = _post(
        {"query": "anything", "rerank": True}, rerank_enabled=False
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "rerank_not_configured"
    rerank_mock.assert_not_awaited()


def test_a_missing_query_wins_over_the_rerank_capability_gate():
    """Same validation order as /api/v1/search: the capability gate runs after
    the query check, so a request failing both reports the query problem. Pinned
    on both endpoints because the shared helper is what makes them agree."""
    resp, _, rerank_mock = _post({"query": "", "rerank": True}, rerank_enabled=False)

    assert resp.status_code == 400
    assert "query" in resp.json()["error"].lower()
    rerank_mock.assert_not_awaited()


def test_rerank_deepens_the_candidate_pool():
    spy = AsyncMock(return_value=[])
    resp, _, _ = _post({"query": "q", "limit": 10, "rerank": True}, search_spy=spy)

    assert resp.status_code == 200
    assert spy.await_args.kwargs["limit"] == 200


def test_the_doc_types_branch_gets_the_deep_pool_too():
    """The per-doc_type loop is a second retrieval path in this handler; a pool
    applied to only one of them reranks a shallow candidate set."""
    spy = AsyncMock(return_value=[])
    resp, _, _ = _post(
        {"query": "q", "limit": 10, "rerank": True, "doc_types": ["file"]},
        search_spy=spy,
    )

    assert resp.status_code == 200
    assert spy.await_args.kwargs["limit"] == 200


def test_without_rerank_the_pool_is_unchanged():
    spy = AsyncMock(return_value=[])
    resp, _, _ = _post({"query": "q", "limit": 10}, search_spy=spy)

    assert resp.status_code == 200
    assert spy.await_args.kwargs["limit"] == 10


def test_the_deep_pool_is_trimmed_back_to_the_requested_limit():
    """Reranking changes the ORDER of the response, never its shape.

    Without the trim a `limit: 5` request would come back with the whole 200-row
    rerank pool — and, with PCA on, 200 plotted points — which reshapes the
    Astrolabe page with no client change and no version signal.
    """
    resp, _, _ = _post({"query": "q", "limit": 5, "rerank": True}, rows=200)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 5
    assert body["total_documents"] == 5


def test_reranked_ordering_is_reported_and_applied():
    def _reorder(results, query, **kwargs):
        # Reverse retrieval order and score it, so an unchanged ordering cannot
        # pass by coincidence.
        reordered = list(reversed(results))
        for i, r in enumerate(reordered):
            r.rerank_score = 1.0 - (i / 10)
        return reordered, RERANK_APPLIED

    resp, _, _ = _post(
        {"query": "q", "limit": 3, "rerank": True},
        rerank_impl=AsyncMock(side_effect=_reorder),
        rows=3,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["reranked"] is True
    assert [r["title"] for r in body["results"]] == ["d2", "d1", "d0"]


def test_rerank_score_is_exposed_when_present():
    def _with_scores(results, query, **kwargs):
        for i, r in enumerate(results):
            r.rerank_score = 0.9 - (i / 10)
        return results, RERANK_APPLIED

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


def test_degraded_rerank_reports_false_and_still_returns_200():
    """Reranking never fails a search; `reranked` is how a caller tells the two
    orderings apart."""
    degraded = AsyncMock(side_effect=lambda r, q, **kw: (r, RERANK_DEGRADED))
    resp, _, _ = _post({"query": "q", "rerank": True}, rerank_impl=degraded, rows=3)

    assert resp.status_code == 200
    assert resp.json()["reranked"] is False
