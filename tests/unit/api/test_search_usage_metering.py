"""Usage metering on POST /api/v1/search.

This endpoint embeds a query — a real billable provider cost — and for a long
time recorded no usage event at all, so Astrolabe-driven search was invisible to
the ledger while MCP-driven search was billed.

The `surface` assertion is the point of this file. `record_search_usage`
defaults `surface="mcp"`, so a call site that simply forgets to pass it produces
events that look *plausible* — they land in the ledger, with a surface field, at
the right volume — while attributing all HTTP traffic to MCP. Nothing downstream
can detect that; only an assertion at the call site can.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import unified_search
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit


def _app() -> Starlette:
    app = Starlette(routes=[Route("/api/v1/search", unified_search, methods=["POST"])])
    app.state.oauth_context = {"config": {"nextcloud_host": "https://nc.example"}}
    return app


def _post(body, *, metering_enabled=True, token_count=42):
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.usage_metering_enabled = metering_enabled
    settings.search_rerank_enabled = False
    settings.embedding_gateway_url = ""

    algo = MagicMock()
    algo.search = AsyncMock(return_value=[])
    algo.query_token_count = token_count
    algo.query_embedding = None

    usage = AsyncMock()
    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings",
            return_value=settings,
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.BM25HybridSearchAlgorithm",
            return_value=algo,
        ),
        patch("nextcloud_mcp_server.api.visualization.record_search_usage", new=usage),
        patch(
            "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
            new=AsyncMock(side_effect=NotProvisionedError("not provisioned")),
        ),
    ):
        return TestClient(_app()).post("/api/v1/search", json=body), usage


def test_http_search_is_metered_as_the_http_surface():
    """The whole point of the surface field: MCP and HTTP must be separable in
    the rollup. `record_search_usage` defaults to "mcp", so omitting this
    argument silently attributes every Astrolabe search to the MCP tool."""
    resp, usage = _post({"query": "anything"})

    assert resp.status_code == 200
    usage.assert_awaited_once()
    assert usage.await_args.kwargs["surface"] == "http"


def test_records_the_query_token_count():
    """Tokens are the billed unit; the value must be the query embedding's
    count, not a placeholder."""
    resp, usage = _post({"query": "anything"}, token_count=137)

    assert resp.status_code == 200
    assert usage.await_args.kwargs["token_count"] == 137


def test_metering_flag_is_forwarded_not_assumed():
    """The helper is flag-gated internally, so the endpoint must pass the real
    setting rather than hardcoding it on."""
    resp, usage = _post({"query": "anything"}, metering_enabled=False)

    assert resp.status_code == 200
    assert usage.await_args.kwargs["enabled"] is False


def test_doc_types_filter_is_recorded_when_a_list():
    resp, usage = _post({"query": "anything", "doc_types": ["file", "note"]})

    assert resp.status_code == 200
    assert usage.await_args.kwargs["doc_types"] == ["file", "note"]


def test_non_list_doc_types_normalize_to_none():
    """Both None and a malformed value must land as null so a future
    `metadata->'doc_types' IS NULL` query counts the all-types case
    consistently."""
    resp, usage = _post({"query": "anything", "doc_types": "file"})

    assert resp.status_code == 200
    assert usage.await_args.kwargs["doc_types"] is None


def _post_metrics(body, *, algo_raises=None):
    """Same harness, but spying on `record_search_request` instead."""
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.usage_metering_enabled = False
    settings.search_rerank_enabled = False
    settings.embedding_gateway_url = ""

    algo = MagicMock()
    algo.search = (
        AsyncMock(side_effect=algo_raises)
        if algo_raises
        else AsyncMock(return_value=[])
    )
    algo.query_token_count = 0
    algo.query_embedding = None

    spy = MagicMock()
    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings",
            return_value=settings,
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.BM25HybridSearchAlgorithm",
            return_value=algo,
        ),
        # `algorithm="semantic"` resolves to the dense-only class, so both must
        # be stubbed or that parametrisation reaches a real search.
        patch(
            "nextcloud_mcp_server.api.visualization.SemanticSearchAlgorithm",
            return_value=algo,
        ),
        patch("nextcloud_mcp_server.api.visualization.record_search_request", new=spy),
        patch(
            "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
            new=AsyncMock(side_effect=NotProvisionedError("not provisioned")),
        ),
    ):
        return TestClient(_app()).post("/api/v1/search", json=body), spy


def test_algorithm_label_is_identical_on_success_and_error():
    """Success and error samples must land on the SAME series.

    Normalising the label on one path and passing the raw value on the other
    fragments `astrolabe_search_requests_total` into non-comparable series and
    breaks "error rate by algorithm" — which is precisely the cross-surface
    drift these metrics exist to detect.
    """
    ok, ok_spy = _post_metrics({"query": "q", "algorithm": "hybrid", "fusion": "rrf"})
    assert ok.status_code == 200
    ok_label = ok_spy.call_args.kwargs["algorithm"]

    bad, bad_spy = _post_metrics(
        {"query": "q", "algorithm": "hybrid", "fusion": "rrf"},
        algo_raises=RuntimeError("qdrant exploded"),
    )
    assert bad.status_code == 500
    err_label = bad_spy.call_args.kwargs["algorithm"]

    assert ok_label == err_label == "bm25_hybrid_rrf"


def test_semantic_algorithm_label_is_not_rewritten():
    """The dense-only algorithm has no fusion, so its label must pass through
    rather than being decorated with a fusion it never used."""
    resp, spy = _post_metrics({"query": "q", "algorithm": "semantic"})

    assert resp.status_code == 200
    assert spy.call_args.kwargs["algorithm"] == "semantic"


def test_error_path_still_records_a_sample():
    """An error must not simply vanish from the request counter."""
    resp, spy = _post_metrics(
        {"query": "q"}, algo_raises=RuntimeError("qdrant exploded")
    )

    assert resp.status_code == 500
    assert spy.call_args.kwargs["status"] == "error"
