"""Keyword-mode gate for the OAuth-session PCA-viz search endpoint
(`nextcloud_mcp_server.auth.viz_routes.vector_visualization_search`).

The pure-dense ``semantic`` algorithm queries the dense vector slot, which a
keyword-only collection (SEARCH_MODE=keyword) does not have. Unlike the
management API endpoints, this route selects the algorithm from a raw query
param, so it must guard ``semantic`` explicitly (ADR-030) rather than letting the
query fail against the sparse-only index. ``bm25_hybrid`` stays valid in keyword
mode (it issues a sparse-only query internally), so it must NOT be rejected.

Auth harness mirrors ``tests/unit/test_viz_routes_chunk_context.py``.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.auth.viz_routes import vector_visualization_search

pytestmark = pytest.mark.unit


class _AlwaysAuthBackend(AuthenticationBackend):
    async def authenticate(self, conn):
        return AuthCredentials(["authenticated"]), SimpleUser("testuser")


def _make_app() -> Starlette:
    return Starlette(
        routes=[
            Route(
                "/app/vector-viz/search",
                vector_visualization_search,
                methods=["GET"],
            ),
        ],
        middleware=[
            # ty flags AuthenticationMiddleware against Middleware's _MiddlewareFactory
            # generic (a starlette typing quirk); CI scopes ty to nextcloud_mcp_server/
            # so this only trips the local pre-commit hook on this test file.
            Middleware(AuthenticationMiddleware, backend=_AlwaysAuthBackend()),  # ty: ignore[invalid-argument-type]
        ],
    )


def _keyword_mode_settings() -> MagicMock:
    """Vector sync on, dense off → SEARCH_MODE=keyword (supported = ["bm25"])."""
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.dense_enabled = False
    settings.get_collection_name.return_value = "test-bm25-keyword"
    return settings


def _mock_auth_client_ctx() -> MagicMock:
    """An async-context-manager mock for _get_authenticated_client_for_userinfo."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def test_semantic_rejected_in_keyword_mode_without_building_dense_algo():
    """An explicit ``algorithm=semantic`` on a keyword-only server → 400, and the
    dense SemanticSearchAlgorithm is never constructed (no dead dense query)."""
    with (
        patch(
            "nextcloud_mcp_server.auth.viz_routes.get_settings",
            return_value=_keyword_mode_settings(),
        ),
        patch(
            "nextcloud_mcp_server.auth.viz_routes._get_authenticated_client_for_userinfo",
            new_callable=AsyncMock,
            return_value=_mock_auth_client_ctx(),
        ),
        patch(
            "nextcloud_mcp_server.auth.viz_routes.SemanticSearchAlgorithm"
        ) as mock_semantic,
    ):
        with TestClient(_make_app()) as client:
            resp = client.get(
                "/app/vector-viz/search?query=leadership&algorithm=semantic"
            )

    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert "keyword-only" in body["error"]
    mock_semantic.assert_not_called()
