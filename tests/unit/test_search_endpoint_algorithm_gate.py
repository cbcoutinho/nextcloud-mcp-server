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


def _keyword_mode_settings() -> MagicMock:
    """Vector sync on, dense off → SEARCH_MODE=keyword advertises only ["bm25"]."""
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.dense_enabled = False
    return settings


def _app() -> Starlette:
    return Starlette(
        routes=[
            Route("/api/v1/search", unified_search, methods=["POST"]),
            Route("/api/v1/vector-viz/search", vector_search, methods=["POST"]),
        ]
    )


@pytest.mark.parametrize("path", ["/api/v1/search", "/api/v1/vector-viz/search"])
# Both dense-requiring algorithms are unsupported in keyword mode (supported is
# ["bm25"]), so each must be rejected — not just "semantic".
@pytest.mark.parametrize("algorithm", ["semantic", "hybrid"])
def test_explicit_dense_algorithm_in_keyword_mode_returns_422(
    path: str, algorithm: str
):
    """An explicit unsupported algorithm is rejected before any Qdrant call."""
    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings",
            return_value=_keyword_mode_settings(),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
    ):
        client = TestClient(_app())
        resp = client.post(
            path, json={"query": "torch leadership award", "algorithm": algorithm}
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_search_type"
    assert body["requested"] == algorithm
    assert body["supported_search_types"] == ["bm25"]


@pytest.mark.parametrize("path", ["/api/v1/search", "/api/v1/vector-viz/search"])
def test_supported_bm25_in_keyword_mode_is_not_rejected(path: str):
    """A supported algorithm passes the gate (it fails later, at the real search,
    which is patched out here — the point is it is NOT a 422 gate rejection)."""
    with (
        patch(
            "nextcloud_mcp_server.api.visualization.get_settings",
            return_value=_keyword_mode_settings(),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new=AsyncMock(return_value=("alice", {})),
        ),
        patch(
            "nextcloud_mcp_server.api.visualization.BM25HybridSearchAlgorithm",
            side_effect=RuntimeError("stop after the gate"),
        ),
    ):
        client = TestClient(_app())
        resp = client.post(
            path, json={"query": "leadership award", "algorithm": "bm25"}
        )

    # Not a 422 from the algorithm gate — the request got past it to the search.
    assert resp.status_code != 422
