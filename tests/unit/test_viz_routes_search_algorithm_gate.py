"""Algorithm selection for the OAuth-session PCA-viz search endpoint
(`nextcloud_mcp_server.auth.viz_routes.vector_visualization_search`).

The old global keyword switch is gone: keyword-vs-hybrid is now per-document and
the collection is always dense-capable, so ``semantic`` (pure dense) is always
valid — the route no longer rejects it. This pins that an explicit
``algorithm=semantic`` now builds the dense ``SemanticSearchAlgorithm`` and
proceeds, rather than being 400'd by the removed "keyword-only" gate.

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


def _vector_sync_on_settings() -> MagicMock:
    """Vector sync on. The collection is always dense-capable, so ``semantic`` is
    always a valid algorithm (no keyword-only gate)."""
    settings = MagicMock()
    settings.vector_sync_enabled = True
    settings.get_collection_name.return_value = "test-collection"
    return settings


def _mock_auth_client_ctx() -> MagicMock:
    """An async-context-manager mock for _get_authenticated_client_for_userinfo."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def test_semantic_is_built_and_not_rejected():
    """An explicit ``algorithm=semantic`` now builds the dense
    SemanticSearchAlgorithm and proceeds — the removed keyword-only gate no longer
    400s it. We stop the handler right after construction (the algorithm's
    ``search`` raises) so the assertion is purely about the gate: the dense algo
    IS constructed, and the response is never the old "keyword-only" 400."""
    with (
        patch(
            "nextcloud_mcp_server.auth.viz_routes.get_settings",
            return_value=_vector_sync_on_settings(),
        ),
        patch(
            "nextcloud_mcp_server.auth.viz_routes._get_authenticated_client_for_userinfo",
            new_callable=AsyncMock,
            return_value=_mock_auth_client_ctx(),
        ),
        patch(
            "nextcloud_mcp_server.auth.viz_routes.SemanticSearchAlgorithm",
            side_effect=RuntimeError("stop after the gate"),
        ) as mock_semantic,
    ):
        with TestClient(_make_app()) as client:
            resp = client.get(
                "/app/vector-viz/search?query=leadership&algorithm=semantic"
            )

    # The dense algorithm IS constructed now (the keyword-only gate is gone).
    mock_semantic.assert_called_once()
    # And the response is never the removed "keyword-only" rejection.
    if resp.status_code == 400:
        assert "keyword-only" not in resp.json().get("error", "")
