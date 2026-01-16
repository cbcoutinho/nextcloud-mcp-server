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
    nextcloud_url: str = "http://localhost",
    enable_token_exchange: bool = False,
    mcp_client_id: str | None = None,
    mcp_client_secret: str | None = None,
):
    """Create mock settings with specified auth configuration."""
    settings = MagicMock()
    settings.enable_multi_user_basic_auth = enable_multi_user_basic
    settings.enable_offline_access = enable_offline_access
    settings.oidc_discovery_url = oidc_discovery_url
    settings.oidc_issuer = oidc_issuer
    settings.vector_sync_enabled = vector_sync_enabled
    settings.nextcloud_url = nextcloud_url
    settings.enable_token_exchange = enable_token_exchange
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
                return_value=AuthMode.OAUTH_SINGLE_AUDIENCE,
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
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
                "nextcloud_mcp_server.config.get_settings", return_value=mock_settings
            ),
            patch(
                "nextcloud_mcp_server.config_validators.detect_auth_mode",
                return_value=AuthMode.SINGLE_USER_BASIC,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/status")

            assert response.status_code == 200
            data = response.json()

            assert data["vector_sync_enabled"] is True
