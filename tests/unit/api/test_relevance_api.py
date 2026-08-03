"""`relevance` on the two HTTP search surfaces (ADR-034).

`tests/unit/search/test_relevance.py` covers the mapping in isolation. These
drive the real Starlette handlers, because the mapping being right says nothing
about the handler passing it the right inputs — which is exactly how the first
version of this shipped a bug: `_build_search_algorithm` normalised `fusion` on
a local copy, so retrieval ran on "rrf" while the response was mapped with the
caller's raw value, and a `{"fusion": null}` body reproduced the very "3%"
reading ADR-034 exists to remove.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import unified_search, vector_search
from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.relevance import (
    RELEVANCE_CALIBRATED,
    RELEVANCE_ORDINAL,
    RELEVANCE_UNCALIBRATED,
)
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit

# 99.2% of the RRF ceiling at k=60 — the score from the Deck #958 report that
# the UI rendered as "3%".
_NEAR_PERFECT_RRF = 0.03306


def _settings():
    s = MagicMock()
    s.vector_sync_enabled = True
    s.usage_metering_enabled = False
    s.search_rerank_enabled = True
    s.embedding_gateway_url = "https://gw.example"
    s.search_rerank_model = "local/BAAI/bge-reranker-v2-m3"
    s.search_rerank_pool_size = 200
    s.search_rerank_timeout_seconds = 30.0
    s.search_rerank_max_concurrency = 1
    return s


def _app(handler, path):
    app = Starlette(routes=[Route(path, handler, methods=["POST"])])
    app.state.oauth_context = {"config": {"nextcloud_host": "https://nc.example"}}
    return app


def _rows(scores, rerank_scores=None):
    return [
        SearchResult(
            id=str(i),
            doc_type="file",
            title=f"d{i}",
            excerpt=f"t{i}",
            score=s,
            rerank_score=(rerank_scores[i] if rerank_scores else None),
        )
        for i, s in enumerate(scores)
    ]


def _post(handler, path, body, *, rows):
    algo = MagicMock()
    algo.search = AsyncMock(return_value=rows)
    algo.query_token_count = 0
    algo.query_embedding = None

    mod = "nextcloud_mcp_server.api.visualization"
    with (
        patch(f"{mod}.get_settings", return_value=_settings()),
        patch(
            f"{mod}.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(f"{mod}.BM25HybridSearchAlgorithm", return_value=algo),
        patch(
            f"{mod}.get_user_client_basic_auth",
            new=AsyncMock(side_effect=NotProvisionedError("not provisioned")),
        ),
    ):
        return TestClient(_app(handler, path)).post(
            path, json={"include_pca": False, **body}
        )


_SURFACES = [
    (unified_search, "/api/v1/search"),
    (vector_search, "/api/v1/vector-viz/search"),
]


@pytest.mark.parametrize(("handler", "path"), _SURFACES)
def test_every_result_carries_relevance_and_its_source(handler, path):
    resp = _post(handler, path, {"query": "q"}, rows=_rows([0.03, 0.02, 0.01]))

    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert rows
    for r in rows:
        assert 0.0 <= r["relevance"] <= 1.0
        assert r["relevance_source"] in {
            RELEVANCE_CALIBRATED,
            RELEVANCE_ORDINAL,
            RELEVANCE_UNCALIBRATED,
        }


@pytest.mark.parametrize(("handler", "path"), _SURFACES)
def test_the_near_perfect_rrf_hit_is_not_reported_as_three_percent(handler, path):
    """The Deck #958 reproducing case, through the real handler."""
    resp = _post(handler, path, {"query": "q"}, rows=_rows([_NEAR_PERFECT_RRF]))

    row = resp.json()["results"][0]
    assert row["relevance"] > 0.5, (
        f"a 99.2%-of-ceiling hit was reported as {row['relevance']:.3f}"
    )
    assert row["relevance_source"] == RELEVANCE_ORDINAL


@pytest.mark.parametrize(("handler", "path"), _SURFACES)
@pytest.mark.parametrize("fusion", [None, "RRF", "nonsense", ""])
def test_a_malformed_fusion_still_maps_through_the_fitted_curve(handler, path, fusion):
    """REGRESSION: retrieval normalises an unusable `fusion` to "rrf", so the
    response must be mapped with the SAME normalised value.

    Before the fix `_build_search_algorithm` normalised a local copy only, so
    the search ran on "rrf" while `relevance` was mapped with the caller's raw
    string — falling through to the uncalibrated branch and emitting the raw
    ~0.03 RRF score. `{"fusion": null}` reaches this because `.get(k, default)`
    returns None when the key is present.
    """
    resp = _post(
        handler, path, {"query": "q", "fusion": fusion}, rows=_rows([_NEAR_PERFECT_RRF])
    )

    row = resp.json()["results"][0]
    assert row["relevance_source"] == RELEVANCE_ORDINAL, (
        f"fusion={fusion!r} bypassed the fitted curve"
    )
    assert row["relevance"] > 0.5


@pytest.mark.parametrize(("handler", "path"), _SURFACES)
def test_dbsf_is_reported_uncalibrated_rather_than_mapped_through_the_rrf_curve(
    handler, path
):
    """DBSF is a different, unbounded scale — the RRF fit does not apply, and
    saying so is the point of `relevance_source`."""
    resp = _post(handler, path, {"query": "q", "fusion": "dbsf"}, rows=_rows([0.8]))

    assert resp.json()["results"][0]["relevance_source"] == RELEVANCE_UNCALIBRATED


@pytest.mark.parametrize(("handler", "path"), _SURFACES)
def test_the_response_publishes_the_prevalence_the_curves_were_fitted_at(handler, path):
    """ADR-034 says the fit base rate ships with the number so a caller can
    reason about which direction a different corpus biases it. That has to be on
    the response, not only importable from the module."""
    resp = _post(handler, path, {"query": "q"}, rows=_rows([0.03]))

    assert resp.json()["relevance_fit_base_rate"] == pytest.approx(0.178)
