"""`granularity` on POST /api/v1/vector-viz/search.

This endpoint is the ONLY one the Astrolabe app's search page calls, and until
now it did not accept `granularity` at all — it passed no value to the search
algorithm and hardcoded `chunk` into its metrics. So `granularity="document"`
(one row per document rather than per passage) was unreachable from the UI, even
though `/api/v1/search` and `nc_semantic_search` both exposed it.

That mattered beyond a missing feature: ADR-034's relevance curves were FITTED
at document granularity, so the app page could not request the retrieval shape
its own relevance numbers were calibrated on.

Sibling of test_vector_viz_rerank_api.py; same handler, same scaffolding. The
contract asserted here is that this endpoint now agrees with /api/v1/search on
all four points: the value is read, an unknown value is rejected rather than
silently downgraded, the document+semantic combination is refused with the same
422 payload, and the value actually reaches the algorithm.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import vector_search
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit


def _settings():
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.search_rerank_enabled = False
    settings.embedding_gateway_url = ""
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


def _post(body, *, rows=2):
    """POST with the algorithm stubbed; returns (response, algo)."""
    algo = MagicMock()
    algo.search = AsyncMock(return_value=_rows(rows))
    algo.query_token_count = 0
    algo.query_embedding = None

    body = {"include_pca": False, **body}

    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings",
            return_value=_settings(),
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
            "nextcloud_mcp_server.api.visualization.SemanticSearchAlgorithm",
            return_value=algo,
        ),
        # Unprovisioned ⇒ the real _execute closure runs, so its kwargs are
        # observable on the algo spy.
        patch(
            "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
            new=AsyncMock(side_effect=NotProvisionedError("not provisioned")),
        ),
    ):
        client = TestClient(_app())
        return client.post("/api/v1/vector-viz/search", json=body), algo


def test_default_granularity_is_chunk():
    """Omitting the field must behave exactly as before it was accepted —
    every existing Astrolabe release sends no value."""
    resp, algo = _post({"query": "anything"})

    assert resp.status_code == 200
    assert algo.search.await_args.kwargs["granularity"] == "chunk"


def test_document_granularity_reaches_the_algorithm():
    """The point of the change: the value is not merely accepted, it is passed
    down. Accepting it and ignoring it would be worse than rejecting it."""
    resp, algo = _post({"query": "anything", "granularity": "document"})

    assert resp.status_code == 200
    assert algo.search.await_args.kwargs["granularity"] == "document"


def test_document_granularity_reaches_the_algorithm_on_the_doc_types_branch():
    """`doc_types` takes a separate loop through the same closure. A parameter
    threaded on one branch and not the other is invisible to anyone who happens
    to filter by type — which the Astrolabe UI does on every search."""
    resp, algo = _post(
        {"query": "anything", "granularity": "document", "doc_types": ["file"]}
    )

    assert resp.status_code == 200
    assert algo.search.await_args.kwargs["granularity"] == "document"


def test_unknown_granularity_is_rejected_with_400():
    """Rejected, not silently downgraded to chunk: a caller that asked for one
    row per document and quietly received passages cannot tell that apart from
    a corpus that genuinely has one chunk per document."""
    resp, algo = _post({"query": "anything", "granularity": "paragraph"})

    assert resp.status_code == 400
    assert "granularity" in resp.json()["error"]
    algo.search.assert_not_awaited()


def test_document_granularity_with_semantic_is_refused_with_422():
    """Grouped retrieval needs a sparse leg to group on, so document
    granularity is unsupported for dense-only search. Same error contract as
    /api/v1/search so a client handles one shape across both endpoints."""
    resp, _ = _post(
        {"query": "anything", "granularity": "document", "algorithm": "semantic"}
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "granularity_unsupported_for_algorithm"
    assert body["granularity"] == "document"
    assert body["algorithm"] == "semantic"
    assert body["supported_algorithms"] == ["bm25", "hybrid"]


def test_chunk_granularity_with_semantic_is_allowed():
    """The 422 is specific to the document+dense combination — dense-only
    passage search is the endpoint's oldest behaviour and must keep working."""
    resp, algo = _post(
        {"query": "anything", "granularity": "chunk", "algorithm": "semantic"}
    )

    assert resp.status_code == 200
    assert algo.search.await_args.kwargs["granularity"] == "chunk"
