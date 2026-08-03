"""Reranking must not multiply the load verify-on-read puts on Nextcloud.

The rerank stage runs BEFORE verification and over a deliberately deep pool, so
whatever it returns is what verify-on-read then checks — one Nextcloud
round-trip per candidate. Without cutting the pool back first, turning on
reranking turns a ~20-candidate verification into a ~200-candidate one.

These drive the PROVISIONED path specifically. The rest of the rerank HTTP tests
use the unprovisioned path, because that is what makes `_execute`'s kwargs
observable — but that path skips `verify_search_results` entirely, so it can
never see the size actually handed to verification. That blind spot is why this
regression reached review.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import unified_search, vector_search
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.rerank import RERANK_APPLIED

pytestmark = pytest.mark.unit

_POOL = 200


def _settings():
    s = MagicMock()
    s.vector_sync_enabled = True
    s.usage_metering_enabled = False
    s.search_rerank_enabled = True
    s.embedding_gateway_url = "https://gw.example"
    s.search_rerank_model = "vendor/model"
    s.search_rerank_pool_size = _POOL
    s.search_rerank_timeout_seconds = 30.0
    s.search_rerank_max_concurrency = 1
    return s


def _app() -> Starlette:
    app = Starlette(routes=[Route("/api/v1/search", unified_search, methods=["POST"])])
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


def _post_provisioned(body):
    """Drive the provisioned branch and capture what reaches verification."""
    verified: dict = {}

    async def _verify(nc_client, results, **kwargs):
        verified["count"] = len(results)
        return results, 0

    algo = MagicMock()

    async def _search(**kwargs):
        # Honour the requested limit: a mock that always returns the full pool
        # would make the unreranked baseline look as large as the reranked one
        # and hide the very difference under test.
        return _rows(min(kwargs.get("limit", 10), _POOL))

    algo.search = AsyncMock(side_effect=_search)
    algo.query_token_count = 0
    algo.query_embedding = None

    # Awaited first, then used as an async context manager, and `.sharing` is
    # read inside it — so the double must support all three.
    client = MagicMock()
    client.sharing = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    scope = MagicMock()
    scope.owners = ["alice"]
    scope.share_root_ids = []

    mod = "nextcloud_mcp_server.api.visualization"
    with (
        patch(f"{mod}.get_settings", return_value=_settings()),
        patch(
            f"{mod}.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(f"{mod}.BM25HybridSearchAlgorithm", return_value=algo),
        patch(
            f"{mod}.rerank_results",
            new=AsyncMock(side_effect=lambda r, q, **kw: (r, RERANK_APPLIED)),
        ),
        patch(f"{mod}.list_accessible_scope", new=AsyncMock(return_value=scope)),
        patch(f"{mod}.verify_search_results", new=_verify),
        patch(
            f"{mod}.get_user_client_basic_auth",
            new=AsyncMock(return_value=client),
        ),
    ):
        resp = TestClient(_app()).post("/api/v1/search", json=body)
    return resp, verified


def test_verification_budget_is_unchanged_by_reranking():
    """The core invariant: enabling reranking changes result ORDER, not how much
    work Nextcloud is asked to do."""
    plain, plain_seen = _post_provisioned({"query": "q", "limit": 10})
    reranked, rerank_seen = _post_provisioned(
        {"query": "q", "limit": 10, "rerank": True}
    )

    assert plain.status_code == 200 and reranked.status_code == 200
    assert rerank_seen["count"] == plain_seen["count"], (
        "reranking must not enlarge the set handed to verify-on-read — that is "
        "one Nextcloud round-trip per candidate"
    )


def test_reranked_pool_does_not_reach_verification():
    """Explicit upper bound, so a regression is legible rather than relative."""
    _, seen = _post_provisioned({"query": "q", "limit": 10, "rerank": True})

    assert seen["count"] < _POOL, (
        f"the full rerank pool ({_POOL}) reached verification; the deep pool "
        "exists for the reranker, not for verify-on-read"
    )


def test_doc_types_branch_is_bounded_by_its_own_budget():
    """The doc_types loop caps at `search_limit * 2` before verification, so the
    cutback must track THAT branch's budget rather than a flat value.

    Asserted as a bound, not equality: an unreranked run with a single doc_type
    produces only `search_limit` rows and never reaches its own 2x cap, so
    demanding equality would pin an incidental count rather than the invariant.
    What must hold is that reranking never pushes verification past the budget
    the branch already allows itself.
    """
    limit = 10
    branch_budget = limit * 2  # search_limit * 2 at offset 0

    reranked, rerank_seen = _post_provisioned(
        {"query": "q", "limit": limit, "doc_types": ["file"], "rerank": True}
    )

    assert reranked.status_code == 200
    assert rerank_seen["count"] <= branch_budget, (
        "reranking pushed verify-on-read past the budget this branch allows "
        "itself even without reranking"
    )
    assert rerank_seen["count"] < _POOL


# --- /api/v1/vector-viz/search ------------------------------------------------
#
# The same invariant on the second search endpoint. It needs its own coverage
# rather than trusting the sibling's: the two handlers are separate code paths
# and the first version of the viz endpoint reranked AFTER _search_with_acl, so
# the whole deep pool went through verification. Symmetric tests are what turn
# that from a review catch into a build failure.


def _viz_app() -> Starlette:
    app = Starlette(
        routes=[Route("/api/v1/vector-viz/search", vector_search, methods=["POST"])]
    )
    app.state.oauth_context = {"config": {"nextcloud_host": "https://nc.example"}}
    return app


def _post_viz_provisioned(body):
    verified: dict = {}

    async def _verify(nc_client, results, **kwargs):
        verified["count"] = len(results)
        return results, 0

    algo = MagicMock()

    async def _search(**kwargs):
        return _rows(min(kwargs.get("limit", 10), _POOL))

    algo.search = AsyncMock(side_effect=_search)
    algo.query_token_count = 0
    algo.query_embedding = None

    client = MagicMock()
    client.sharing = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    scope = MagicMock()
    scope.owners = ["alice"]
    scope.share_root_ids = []

    # PCA off: it is orthogonal to the verification budget and would pull an
    # embedding provider into the test.
    body = {"include_pca": False, **body}

    mod = "nextcloud_mcp_server.api.visualization"
    with (
        patch(f"{mod}.get_settings", return_value=_settings()),
        patch(
            f"{mod}.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(f"{mod}.BM25HybridSearchAlgorithm", return_value=algo),
        patch(
            f"{mod}.rerank_results",
            new=AsyncMock(side_effect=lambda r, q, **kw: (r, RERANK_APPLIED)),
        ),
        patch(f"{mod}.list_accessible_scope", new=AsyncMock(return_value=scope)),
        patch(f"{mod}.verify_search_results", new=_verify),
        patch(f"{mod}.get_user_client_basic_auth", new=AsyncMock(return_value=client)),
    ):
        resp = TestClient(_viz_app()).post("/api/v1/vector-viz/search", json=body)
    return resp, verified


def test_viz_verification_budget_is_unchanged_by_reranking():
    plain, plain_seen = _post_viz_provisioned({"query": "q", "limit": 10})
    reranked, rerank_seen = _post_viz_provisioned(
        {"query": "q", "limit": 10, "rerank": True}
    )

    assert plain.status_code == 200 and reranked.status_code == 200
    assert rerank_seen["count"] == plain_seen["count"], (
        "reranking must not enlarge the set handed to verify-on-read — that is "
        "one Nextcloud round-trip per candidate"
    )


def test_viz_reranked_pool_does_not_reach_verification():
    _, seen = _post_viz_provisioned({"query": "q", "limit": 10, "rerank": True})

    assert seen["count"] < _POOL, (
        f"the full rerank pool ({_POOL}) reached verification; the deep pool "
        "exists for the reranker, not for verify-on-read"
    )


def test_viz_doc_types_branch_does_not_multiply_verification():
    """The per-doc_type loop fetches the pool once per type, so an untrimmed
    pool would hand verification len(doc_types) x pool rows."""
    _, seen = _post_viz_provisioned(
        {"query": "q", "limit": 10, "doc_types": ["file", "note"], "rerank": True}
    )

    assert seen["count"] <= 10, (
        "verification saw more than the caller's limit; the deep pool must be "
        "cut back inside _execute, before verify-on-read"
    )
