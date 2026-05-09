"""Unit tests for the OAuth-session chunk-context endpoint
(`nextcloud_mcp_server.auth.viz_routes.chunk_context_endpoint`).

Mirrors the regression coverage of
`tests/unit/test_management_chunk_context_endpoint.py` (which targets the
management API route in `nextcloud_mcp_server.api.visualization`).

Both routes share the same purpose — fetch chunk text with surrounding
context for the viz pane — but live behind different auth surfaces:

* Management API: OAuth bearer validated by `validate_token_and_get_user`
* Viz route: Starlette session auth via `@requires("authenticated")`

Because of the auth-middleware difference, a separate file is cleaner than
mixing both styles into one test module.
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

from nextcloud_mcp_server.auth.viz_routes import chunk_context_endpoint

pytestmark = pytest.mark.unit


class _AlwaysAuthBackend(AuthenticationBackend):
    """Stub auth backend: every request is authenticated as `testuser`."""

    async def authenticate(self, conn):
        return AuthCredentials(["authenticated"]), SimpleUser("testuser")


def _make_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/app/chunk-context", chunk_context_endpoint, methods=["GET"]),
        ],
        middleware=[
            Middleware(AuthenticationMiddleware, backend=_AlwaysAuthBackend()),
        ],
    )


def _make_mock_chunk_context(chunk_text="chunk", before="before", after="after"):
    """Mock a ChunkContext dataclass with enough fields for the handler."""
    ctx = MagicMock()
    ctx.chunk_text = chunk_text
    ctx.before_context = before
    ctx.after_context = after
    ctx.has_before_truncation = False
    ctx.has_after_truncation = False
    ctx.page_number = None
    ctx.chunk_index = 0
    ctx.total_chunks = 1
    return ctx


def _make_mock_nc_client():
    """Mock NextcloudClient that supports `async with`."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def _make_mock_settings(nextcloud_host: str = "http://localhost:8080") -> MagicMock:
    """Mock get_settings() return value with the fields the handler reads."""
    settings = MagicMock()
    settings.nextcloud_host = nextcloud_host
    settings.get_collection_name.return_value = "test-collection"
    return settings


class TestVizChunkContextParameterForwarding:
    """Regression guard mirroring TestChunkContextParameterForwarding for the
    management API: chunk_index / total_chunks must reach get_chunk_with_context.
    """

    def test_chunk_index_and_total_chunks_forwarded(self):
        mock_nc_client = _make_mock_nc_client()
        mock_ctx = _make_mock_chunk_context()
        mock_ctx.chunk_index = 7
        mock_ctx.total_chunks = 10

        with (
            patch(
                "nextcloud_mcp_server.auth.viz_routes.get_settings",
                return_value=_make_mock_settings(),
            ),
            patch(
                "nextcloud_mcp_server.auth.viz_routes.get_user_client_basic_auth",
                new_callable=AsyncMock,
                return_value=mock_nc_client,
            ),
            patch(
                "nextcloud_mcp_server.auth.viz_routes.get_chunk_with_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ) as mock_get_chunk,
        ):
            with TestClient(_make_app()) as client:
                response = client.get(
                    "/app/chunk-context?doc_type=note&doc_id=42"
                    "&start=0&end=10&chunk_index=7&total_chunks=10"
                )

            assert response.status_code == 200
            kwargs = mock_get_chunk.await_args.kwargs
            assert kwargs["chunk_index"] == 7
            assert kwargs["total_chunks"] == 10

            data = response.json()
            assert data["chunk_index"] == 7
            assert data["total_chunks"] == 10
            # page_number must be present even when None — the response shape
            # is unconditional so the frontend can rely on the key existing.
            assert "page_number" in data


class TestVizChunkContextFile404:
    """When get_chunk_with_context returns None for doc_type=file, the route
    must surface a fast 404 — no slow PDF re-parse fallback. This guards the
    proxy-timeout fix from PR #767.
    """

    def test_file_doc_type_qdrant_miss_yields_fast_404(self):
        mock_nc_client = _make_mock_nc_client()

        with (
            patch(
                "nextcloud_mcp_server.auth.viz_routes.get_settings",
                return_value=_make_mock_settings(),
            ),
            patch(
                "nextcloud_mcp_server.auth.viz_routes.get_user_client_basic_auth",
                new_callable=AsyncMock,
                return_value=mock_nc_client,
            ),
            patch(
                "nextcloud_mcp_server.auth.viz_routes.get_chunk_with_context",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_get_chunk,
        ):
            with TestClient(_make_app()) as client:
                response = client.get(
                    "/app/chunk-context?doc_type=file&doc_id=12345"
                    "&start=0&end=10&chunk_index=3&total_chunks=20"
                )

            assert response.status_code == 404
            data = response.json()
            assert data["success"] is False
            assert "failed to fetch chunk context" in data["error"].lower()

            kwargs = mock_get_chunk.await_args.kwargs
            assert kwargs["doc_type"] == "file"
            assert kwargs["chunk_index"] == 3
            assert kwargs["total_chunks"] == 20


class TestVizChunkContextValueErrorLogging:
    """Verify the route returns 400 for malformed integer params (and does
    not crash with a 500). The log-level demotion (logger.warning) is
    asserted indirectly via response shape — log-level itself is not a
    behaviour the user can observe through HTTP.
    """

    def test_invalid_int_param_returns_400(self):
        with patch(
            "nextcloud_mcp_server.auth.viz_routes.get_settings",
            return_value=_make_mock_settings(),
        ):
            with TestClient(_make_app()) as client:
                response = client.get(
                    "/app/chunk-context?doc_type=note&doc_id=1"
                    "&start=not-a-number&end=10"
                )

        assert response.status_code == 400
        data = response.json()
        assert data["success"] is False
        assert "invalid" in data["error"].lower()
