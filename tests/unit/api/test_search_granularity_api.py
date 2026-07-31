"""`granularity` on POST /api/v1/search — the surface Astrolabe consumes.

The MCP tool's granularity behaviour is covered in
``tests/unit/search/test_bm25_hybrid.py`` and
``tests/integration/test_search_granularity.py``. These drive the real
Starlette handler so the HTTP contract itself is pinned: the default, the
pass-through to the algorithm, and the two rejection paths.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import unified_search
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.vector_sync_enabled = True
    return settings


def _app() -> Starlette:
    app = Starlette(routes=[Route("/api/v1/search", unified_search, methods=["POST"])])
    # _search_with_acl reads the configured Nextcloud host from here.
    app.state.oauth_context = {"config": {"nextcloud_host": "https://nc.example"}}
    return app


def _post(body: dict, *, search_spy=None):
    algo = MagicMock()
    algo.search = search_spy or AsyncMock(return_value=[])
    algo.query_token_count = 0
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
        # Let _search_with_acl actually run: an unprovisioned caller takes the
        # execute(None) path, so the real _execute closure runs and we can
        # observe the kwargs it forwards to the algorithm.
        patch(
            "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
            new=AsyncMock(side_effect=NotProvisionedError("not provisioned")),
        ),
    ):
        return TestClient(_app()).post("/api/v1/search", json=body), algo


def test_invalid_granularity_is_rejected_with_400():
    """Rejected rather than normalised: silently downgrading a 'document'
    request to chunks would surface as a ranking bug, not a bad request."""
    resp, _ = _post({"query": "anything", "granularity": "documnet"})

    assert resp.status_code == 400
    assert "Invalid granularity" in resp.json()["error"]


def test_document_granularity_with_semantic_algorithm_returns_422():
    """The dense-only algorithm has no grouping and would ignore the kwarg."""
    resp, _ = _post(
        {"query": "anything", "algorithm": "semantic", "granularity": "document"}
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "granularity_unsupported_for_algorithm"
    assert body["algorithm"] == "semantic"
    assert body["supported_algorithms"] == ["bm25", "hybrid"]


@pytest.mark.parametrize("algorithm", ["bm25", "hybrid"])
def test_document_granularity_reaches_the_algorithm(algorithm: str):
    """Accepted *and* forwarded — a 200 alone would not prove the wiring."""
    spy = AsyncMock(return_value=[])
    resp, _ = _post(
        {"query": "anything", "algorithm": algorithm, "granularity": "document"},
        search_spy=spy,
    )

    assert resp.status_code == 200
    spy.assert_awaited()
    assert spy.await_args.kwargs["granularity"] == "document"


def test_default_granularity_is_chunk():
    """Omitting the field must leave existing callers on today's behaviour."""
    spy = AsyncMock(return_value=[])
    resp, _ = _post({"query": "anything"}, search_spy=spy)

    assert resp.status_code == 200
    spy.assert_awaited()
    assert spy.await_args.kwargs["granularity"] == "chunk"
