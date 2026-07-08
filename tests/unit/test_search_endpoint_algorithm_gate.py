"""Endpoint wiring for the strict search-algorithm gate (ADR-030).

The pure ``select_search_algorithm`` helper is unit-tested in
``test_management_status_endpoint.py``; these tests drive the real Starlette
handlers (``unified_search`` → /api/v1/search, ``vector_search`` →
/api/v1/vector-viz/search) through a TestClient so the actual
``try/except UnsupportedSearchType → 422`` wiring — and the shared
``_unsupported_search_type_response`` payload — is covered locally, not only by
the provider-verification job.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import unified_search, vector_search

pytestmark = pytest.mark.unit


def _vector_sync_on_settings() -> MagicMock:
    """Vector sync on → the collection is always dense-capable, so all three
    algorithms are supported. The old per-instance keyword switch is gone, so a
    dense-requiring algorithm is never rejected on dense grounds; the only
    endpoint-level 422 left is an *unknown* algorithm value.
    """
    settings = MagicMock()
    settings.vector_sync_enabled = True
    return settings


def _app() -> Starlette:
    return Starlette(
        routes=[
            Route("/api/v1/search", unified_search, methods=["POST"]),
            Route("/api/v1/vector-viz/search", vector_search, methods=["POST"]),
        ]
    )


@pytest.mark.parametrize("path", ["/api/v1/search", "/api/v1/vector-viz/search"])
def test_explicit_unknown_algorithm_returns_422(path: str):
    """An explicit *unknown* algorithm is rejected at the gate before any Qdrant
    call, carrying the advertised supported set for a self-correcting 422. (Vector
    sync off short-circuits to 404 earlier, so this is the only reachable 422 at
    the endpoint level now that semantic/bm25/hybrid are always supported.)"""
    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings",
            return_value=_vector_sync_on_settings(),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
    ):
        client = TestClient(_app())
        resp = client.post(
            path, json={"query": "torch leadership award", "algorithm": "fulltext"}
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_search_type"
    assert body["requested"] == "fulltext"
    assert body["supported_search_types"] == ["semantic", "bm25", "hybrid"]


@pytest.mark.parametrize("path", ["/api/v1/search", "/api/v1/vector-viz/search"])
@pytest.mark.parametrize("algorithm", ["semantic", "bm25", "hybrid"])
def test_supported_algorithm_is_not_rejected(path: str, algorithm: str):
    """When vector sync is on, every advertised algorithm passes the gate (it
    fails later, at the real search, which is patched out here — the point is it
    is NOT a 422 gate rejection)."""
    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings",
            return_value=_vector_sync_on_settings(),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.BM25HybridSearchAlgorithm",
            side_effect=RuntimeError("stop after the gate"),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.SemanticSearchAlgorithm",
            side_effect=RuntimeError("stop after the gate"),
        ),
    ):
        client = TestClient(_app())
        resp = client.post(
            path, json={"query": "leadership award", "algorithm": algorithm}
        )

    # Not a 422 from the algorithm gate — the request got past it to the search.
    assert resp.status_code != 422
