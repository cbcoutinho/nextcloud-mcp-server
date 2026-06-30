"""
Unit tests for Management API status endpoint.

Tests the /api/v1/status endpoint focusing on:
- OIDC config availability in different auth modes
- Hybrid mode (multi_user_basic + enable_offline_access) returning OIDC config
- OAuth mode returning OIDC config
- Non-OAuth modes NOT returning OIDC config
"""

from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.management import get_server_status
from nextcloud_mcp_server.config_validators import AuthMode

pytestmark = pytest.mark.unit


def create_test_app():
    """Create a test Starlette app with the status endpoint."""
    return Starlette(
        routes=[
            Route("/api/v1/status", get_server_status, methods=["GET"]),
        ]
    )


def create_mock_settings(
    enable_multi_user_basic: bool = False,
    enable_offline_access: bool = False,
    oidc_discovery_url: str | None = None,
    oidc_issuer: str | None = None,
    vector_sync_enabled: bool = False,
    webhook_secret: str | None = None,
    nextcloud_url: str = "http://localhost",
    mcp_client_id: str | None = None,
    mcp_client_secret: str | None = None,
    dense_enabled: bool = True,
):
    """Create mock settings with specified auth configuration."""
    settings = MagicMock()
    settings.enable_multi_user_basic_auth = enable_multi_user_basic
    settings.enable_offline_access = enable_offline_access
    settings.oidc_discovery_url = oidc_discovery_url
    settings.oidc_issuer = oidc_issuer
    settings.vector_sync_enabled = vector_sync_enabled
    # dense_enabled is False only in SEARCH_MODE=keyword (ADR-030); drives the
    # supported_search_types advertised by /api/v1/status.
    settings.dense_enabled = dense_enabled
    # Explicit so bool(settings.webhook_secret) is deterministic (a bare
    # MagicMock attribute is truthy, which would always report webhooks on).
    settings.webhook_secret = webhook_secret
    settings.nextcloud_url = nextcloud_url
    settings.mcp_client_id = mcp_client_id
    settings.mcp_client_secret = mcp_client_secret
    return settings


class TestStatusEndpointOidcConfig:
    """Tests for OIDC configuration in status endpoint."""

    def test_hybrid_mode_returns_oidc_config(self):
        """Test that hybrid mode (multi_user_basic + offline_access) returns OIDC config."""
        mock_settings = create_mock_settings(
            enable_multi_user_basic=True,
            enable_offline_access=True,
            oidc_discovery_url="http://keycloak/.well-known/openid-configuration",
            oidc_issuer="http://keycloak/realms/test",
        )

        # get_settings and detect_auth_mode are imported inside the function
        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.MULTI_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            # Verify auth mode
            assert data["auth_mode"] == "multi_user_basic"
            assert data["supports_app_passwords"] is True

            # Verify OIDC config is present (key feature for hybrid mode)
            assert "oidc" in data
            assert (
                data["oidc"]["discovery_url"]
                == "http://keycloak/.well-known/openid-configuration"
            )
            assert data["oidc"]["issuer"] == "http://keycloak/realms/test"

    def test_hybrid_mode_without_oidc_settings_no_oidc_key(self):
        """Test that hybrid mode without OIDC settings doesn't include empty oidc key."""
        mock_settings = create_mock_settings(
            enable_multi_user_basic=True,
            enable_offline_access=True,
            oidc_discovery_url=None,
            oidc_issuer=None,
        )

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.MULTI_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            # OIDC key should NOT be present if no OIDC settings configured
            assert "oidc" not in data

    def test_multi_user_basic_without_offline_access_no_oidc(self):
        """Test that multi_user_basic WITHOUT offline_access doesn't return OIDC config."""
        mock_settings = create_mock_settings(
            enable_multi_user_basic=True,
            enable_offline_access=False,  # Key difference: no offline access
            oidc_discovery_url="http://keycloak/.well-known/openid-configuration",
            oidc_issuer="http://keycloak/realms/test",
        )

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.MULTI_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            # Verify auth mode
            assert data["auth_mode"] == "multi_user_basic"
            assert data["supports_app_passwords"] is False

            # OIDC config should NOT be present (not hybrid mode)
            assert "oidc" not in data

    def test_oauth_mode_returns_oidc_config(self):
        """Test that OAuth mode returns OIDC config."""
        mock_settings = create_mock_settings(
            enable_multi_user_basic=False,
            enable_offline_access=True,
            oidc_discovery_url="http://nextcloud/.well-known/openid-configuration",
            oidc_issuer="http://nextcloud",
        )

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.LOGIN_FLOW,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            # Verify auth mode
            assert data["auth_mode"] == "oauth"

            # Verify OIDC config is present
            assert "oidc" in data
            assert (
                data["oidc"]["discovery_url"]
                == "http://nextcloud/.well-known/openid-configuration"
            )

    def test_single_user_basic_no_oidc(self):
        """Test that single-user BasicAuth mode doesn't return OIDC config."""
        mock_settings = create_mock_settings(
            enable_multi_user_basic=False,
            enable_offline_access=False,
            oidc_discovery_url="http://keycloak/.well-known/openid-configuration",
            oidc_issuer="http://keycloak/realms/test",
        )

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.SINGLE_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            # Verify auth mode
            assert data["auth_mode"] == "basic"

            # OIDC config should NOT be present
            assert "oidc" not in data
            # supports_app_passwords should NOT be present (only for multi_user_basic)
            assert "supports_app_passwords" not in data

    def test_oidc_partial_config_only_discovery_url(self):
        """Test OIDC config with only discovery URL set."""
        mock_settings = create_mock_settings(
            enable_multi_user_basic=True,
            enable_offline_access=True,
            oidc_discovery_url="http://keycloak/.well-known/openid-configuration",
            oidc_issuer=None,  # Only discovery URL
        )

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.MULTI_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            assert "oidc" in data
            assert (
                data["oidc"]["discovery_url"]
                == "http://keycloak/.well-known/openid-configuration"
            )
            assert "issuer" not in data["oidc"]

    def test_oidc_partial_config_only_issuer(self):
        """Test OIDC config with only issuer set."""
        mock_settings = create_mock_settings(
            enable_multi_user_basic=True,
            enable_offline_access=True,
            oidc_discovery_url=None,  # Only issuer
            oidc_issuer="http://keycloak/realms/test",
        )

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.MULTI_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            assert "oidc" in data
            assert "discovery_url" not in data["oidc"]
            assert data["oidc"]["issuer"] == "http://keycloak/realms/test"


class TestStatusEndpointBasicResponse:
    """Tests for basic status endpoint response fields."""

    def test_status_includes_version(self):
        """Test that status endpoint includes version."""
        mock_settings = create_mock_settings()

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.SINGLE_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            assert "version" in data
            assert "uptime_seconds" in data
            assert "management_api_version" in data
            assert data["management_api_version"] == "1.0"

    def test_status_includes_vector_sync_enabled(self):
        """Test that status endpoint includes vector_sync_enabled."""
        mock_settings = create_mock_settings(vector_sync_enabled=True)

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.SINGLE_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            assert data["vector_sync_enabled"] is True

    def test_status_reports_webhooks_enabled_when_secret_set(self):
        """webhooks_enabled is True when WEBHOOK_SECRET is configured."""
        mock_settings = create_mock_settings(webhook_secret="supersecret")

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.SINGLE_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            assert response.json()["webhooks_enabled"] is True

    def test_status_reports_webhooks_disabled_when_secret_unset(self):
        """webhooks_enabled is False when WEBHOOK_SECRET is unset (default).

        Security (GHSA-8vh3-g2qg-2h2c): the receiver route is not mounted
        without a secret, so the Astrolabe UI can surface webhooks as off."""
        mock_settings = create_mock_settings(webhook_secret=None)

        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.SINGLE_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            assert response.json()["webhooks_enabled"] is False


class TestSupportedSearchTypesHelper:
    """Pure helper: supported_search_types(settings) (ADR-030)."""

    def test_vector_sync_disabled_is_empty(self):
        from nextcloud_mcp_server.api.management import supported_search_types

        s = create_mock_settings(vector_sync_enabled=False, dense_enabled=True)
        assert supported_search_types(s) == []

    def test_hybrid_mode_advertises_all_three(self):
        from nextcloud_mcp_server.api.management import supported_search_types

        s = create_mock_settings(vector_sync_enabled=True, dense_enabled=True)
        assert supported_search_types(s) == ["semantic", "bm25", "hybrid"]

    def test_keyword_mode_advertises_bm25_only(self):
        from nextcloud_mcp_server.api.management import supported_search_types

        s = create_mock_settings(vector_sync_enabled=True, dense_enabled=False)
        assert supported_search_types(s) == ["bm25"]


class TestResolveSearchAlgorithm:
    """resolve_search_algorithm coerces requests to a mode-serviceable one."""

    def test_hybrid_mode_passes_through_valid(self):
        from nextcloud_mcp_server.api.management import resolve_search_algorithm

        s = create_mock_settings(vector_sync_enabled=True, dense_enabled=True)
        for algo in ("semantic", "bm25", "hybrid"):
            assert resolve_search_algorithm(algo, s) == algo

    def test_unknown_algorithm_falls_back_to_hybrid(self):
        from nextcloud_mcp_server.api.management import resolve_search_algorithm

        s = create_mock_settings(vector_sync_enabled=True, dense_enabled=True)
        assert resolve_search_algorithm("nonsense", s) == "hybrid"

    def test_keyword_mode_redirects_dense_requests_to_bm25(self):
        from nextcloud_mcp_server.api.management import resolve_search_algorithm

        s = create_mock_settings(vector_sync_enabled=True, dense_enabled=False)
        # "semantic" would route a dense query at a sparse-only index → bm25.
        assert resolve_search_algorithm("semantic", s) == "bm25"
        assert resolve_search_algorithm("hybrid", s) == "bm25"
        assert resolve_search_algorithm("bm25", s) == "bm25"

    def test_vector_sync_off_preserves_hybrid_default(self):
        from nextcloud_mcp_server.api.management import resolve_search_algorithm

        s = create_mock_settings(vector_sync_enabled=False)
        assert resolve_search_algorithm("semantic", s) == "hybrid"


class TestStatusEndpointSearchTypes:
    """The /api/v1/status response advertises supported_search_types so the
    astrolabe UI can gate its query-type picker (ADR-030)."""

    def _get_status(self, mock_settings):
        with (
            patch(
                "nextcloud_mcp_server.api.management.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "nextcloud_mcp_server.api.management.detect_auth_mode",
                return_value=AuthMode.SINGLE_USER_BASIC,
            ),
        ):
            client = TestClient(create_test_app())
            response = client.get("/api/v1/status")
        assert response.status_code == 200
        return response.json()

    def test_hybrid_mode(self):
        data = self._get_status(
            create_mock_settings(vector_sync_enabled=True, dense_enabled=True)
        )
        assert data["supported_search_types"] == ["semantic", "bm25", "hybrid"]

    def test_keyword_mode(self):
        data = self._get_status(
            create_mock_settings(vector_sync_enabled=True, dense_enabled=False)
        )
        assert data["supported_search_types"] == ["bm25"]

    def test_vector_sync_off(self):
        data = self._get_status(create_mock_settings(vector_sync_enabled=False))
        assert data["supported_search_types"] == []
