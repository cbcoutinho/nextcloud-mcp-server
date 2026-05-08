"""
Unit tests for Management API chunk-context endpoint.

Tests the /api/v1/chunk-context endpoint focusing on:
- Parameter validation (doc_type, doc_id, start, end, context)
- OAuth token validation
- Nextcloud credential path (must use get_user_client_basic_auth, not
  NextcloudClient.from_token — see regression in api/visualization.py)
- Error handling (missing params, invalid ranges, missing credentials)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import get_chunk_context
from nextcloud_mcp_server.vector.oauth_sync import NotProvisionedError

pytestmark = pytest.mark.unit


def create_test_app():
    """Create a test Starlette app with the chunk-context endpoint."""
    app = Starlette(
        routes=[
            Route("/api/v1/chunk-context", get_chunk_context, methods=["GET"]),
        ]
    )
    app.state.oauth_context = {"config": {"nextcloud_host": "http://localhost:8080"}}
    return app


def _make_mock_chunk_context(chunk_text="chunk", before="before", after="after"):
    """Mock the ChunkContext dataclass with enough fields for the handler."""
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


class TestChunkContextParameterValidation:
    """Tests for parameter validation in the chunk-context endpoint."""

    def test_missing_params_returns_400(self):
        with patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new_callable=AsyncMock,
            return_value=("testuser", True),
        ):
            app = create_test_app()
            client = TestClient(app)
            # Missing end
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=1&start=0",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
            assert "required parameters" in data["error"].lower()

    def test_end_less_than_or_equal_start_returns_400(self):
        with patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new_callable=AsyncMock,
            return_value=("testuser", True),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=1&start=100&end=100",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
            assert "end must be greater than start" in data["error"].lower()

    def test_non_numeric_start_returns_400(self):
        with patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new_callable=AsyncMock,
            return_value=("testuser", True),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=1&start=abc&end=10",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 400


class TestChunkContextTokenValidation:
    """Tests for OAuth token validation."""

    def test_missing_token_returns_401(self):
        with patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new_callable=AsyncMock,
            side_effect=ValueError("Missing Authorization header"),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=1&start=0&end=10"
            )
            assert response.status_code == 401
            data = response.json()
            assert data["error"] == "Unauthorized"

    def test_invalid_token_returns_401(self):
        with patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new_callable=AsyncMock,
            side_effect=Exception("Token expired"),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=1&start=0&end=10",
                headers={"Authorization": "Bearer invalid-token"},
            )
            assert response.status_code == 401


class TestChunkContextCredentialPath:
    """Regression tests: the handler MUST use get_user_client_basic_auth.

    The earlier bug (reproduced in homelab 2026-04-22) forwarded the OAuth
    bearer directly to Nextcloud via NextcloudClient.from_token. That works in
    single-user env-BasicAuth mode but fails with 401 in multi-user BasicAuth
    mode because Nextcloud won't validate that bearer on the Notes API.
    """

    def test_successful_fetch_uses_app_password_client(self):
        """Handler must build its NC client via get_user_client_basic_auth and
        return the chunk text from get_chunk_with_context."""
        mock_nc_client = _make_mock_nc_client()
        mock_ctx = _make_mock_chunk_context(
            chunk_text="hello world",
            before="before the chunk",
            after="after the chunk",
        )

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
                new_callable=AsyncMock,
                return_value=mock_nc_client,
            ) as mock_basic_auth,
            patch(
                "nextcloud_mcp_server.api.visualization.get_chunk_with_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=42&start=0&end=11",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["chunk_text"] == "hello world"
            assert data["before_context"] == "before the chunk"
            assert data["after_context"] == "after the chunk"

            # Regression guard: credential resolution must go through the
            # app-password helper, not NextcloudClient.from_token.
            mock_basic_auth.assert_awaited_once_with(
                "testuser", "http://localhost:8080"
            )

    def test_not_provisioned_returns_401(self):
        """If the user has no stored app password, the handler must surface
        NotProvisionedError as a clean 401 (not 500)."""
        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
                new_callable=AsyncMock,
                side_effect=NotProvisionedError(
                    "User testuser has not provisioned an app password."
                ),
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=1&start=0&end=10",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 401
            data = response.json()
            assert data["success"] is False
            assert "app password" in data["error"].lower()

    def test_chunk_fetch_returns_none_yields_404(self):
        """When get_chunk_with_context returns None (doc missing / offsets
        out of range), the handler reports 404 with a structured error body."""
        mock_nc_client = _make_mock_nc_client()

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
                new_callable=AsyncMock,
                return_value=mock_nc_client,
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.get_chunk_with_context",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=999&start=0&end=10",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 404
            data = response.json()
            assert data["success"] is False
            assert "failed to fetch chunk context" in data["error"].lower()

    def test_file_doc_type_qdrant_miss_yields_fast_404(self):
        """For doc_type=file, a Qdrant miss must surface as 404 immediately
        (no slow PDF re-parse fallback). Locks the proxy-timeout fix in.

        At the unit level we only assert the response shape; the
        no-fallback contract itself lives in `search/context.py` and is
        exercised by chunk-context tests there.
        """
        mock_nc_client = _make_mock_nc_client()

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
                new_callable=AsyncMock,
                return_value=mock_nc_client,
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.get_chunk_with_context",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_get_chunk,
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=file&doc_id=12345"
                "&start=0&end=10&chunk_index=3&total_chunks=20",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 404
            data = response.json()
            assert data["success"] is False
            # Confirm the handler called the resolver with doc_type=file
            # (not a coerced/normalized value) so the fast-fail path engages.
            kwargs = mock_get_chunk.await_args.kwargs
            assert kwargs["doc_type"] == "file"
            assert kwargs["chunk_index"] == 3


class TestChunkContextParameterForwarding:
    """Verify new chunk_index / total_chunks query params reach the lookup.

    Regression guard for PR #767: the whole point of the fix is that callers
    pass chunk_index, and it must arrive at get_chunk_with_context as the
    primary Qdrant lookup key.
    """

    def test_chunk_index_and_total_chunks_forwarded(self):
        mock_nc_client = _make_mock_nc_client()
        mock_ctx = _make_mock_chunk_context()
        mock_ctx.chunk_index = 7
        mock_ctx.total_chunks = 10

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.get_user_client_basic_auth",
                new_callable=AsyncMock,
                return_value=mock_nc_client,
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.get_chunk_with_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ) as mock_get_chunk,
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=42"
                "&start=0&end=10&chunk_index=7&total_chunks=10",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 200
            kwargs = mock_get_chunk.await_args.kwargs
            assert kwargs["chunk_index"] == 7
            assert kwargs["total_chunks"] == 10

            data = response.json()
            assert data["chunk_index"] == 7
            assert data["total_chunks"] == 10
            # page_number must be present even when None (frontend may scroll
            # by it for non-file doc types)
            assert "page_number" in data


class TestChunkContextConfigErrors:
    """Tests for configuration failure paths."""

    def test_missing_nextcloud_host_config(self):
        with patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new_callable=AsyncMock,
            return_value=("testuser", True),
        ):
            app = create_test_app()
            app.state.oauth_context = {"config": {"nextcloud_host": ""}}
            client = TestClient(app)
            response = client.get(
                "/api/v1/chunk-context?doc_type=note&doc_id=1&start=0&end=10",
                headers={"Authorization": "Bearer test-token"},
            )
            # Handler wraps ValueError via _sanitize_error_for_client → 500
            assert response.status_code == 500
