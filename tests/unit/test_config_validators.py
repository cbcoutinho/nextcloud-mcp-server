"""Unit tests for configuration validation and mode detection.

Tests cover:
- Mode detection logic
- Configuration validation for each mode
- Error message generation
- Edge cases and boundary conditions
"""

import os
from unittest.mock import patch

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.config_validators import (
    AuthMode,
    detect_auth_mode,
    get_mode_summary,
    validate_configuration,
)


class TestModeDetection:
    """Test auth mode detection from configuration."""

    def test_smithery_mode_detection(self):
        """Test Smithery mode is detected from environment variable."""
        settings = Settings()

        with patch.dict(os.environ, {"SMITHERY_DEPLOYMENT": "true"}):
            mode = detect_auth_mode(settings)
            assert mode == AuthMode.SMITHERY_STATELESS

    def test_token_exchange_mode_detection(self):
        """Test token exchange mode is detected."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_token_exchange=True,
        )

        mode = detect_auth_mode(settings)
        assert mode == AuthMode.OAUTH_TOKEN_EXCHANGE

    def test_multi_user_basic_mode_detection(self):
        """Test multi-user BasicAuth mode is detected."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_multi_user_basic_auth=True,
        )

        mode = detect_auth_mode(settings)
        assert mode == AuthMode.MULTI_USER_BASIC

    def test_single_user_basic_mode_detection(self):
        """Test single-user BasicAuth mode is detected."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
        )

        mode = detect_auth_mode(settings)
        assert mode == AuthMode.SINGLE_USER_BASIC

    def test_oauth_single_audience_default(self):
        """Test OAuth single-audience is default mode."""
        settings = Settings(
            nextcloud_host="http://localhost",
        )

        mode = detect_auth_mode(settings)
        assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE

    def test_mode_priority_smithery_over_all(self):
        """Test Smithery mode has highest priority."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
            enable_token_exchange=True,
            enable_multi_user_basic_auth=True,
        )

        with patch.dict(os.environ, {"SMITHERY_DEPLOYMENT": "true"}):
            mode = detect_auth_mode(settings)
            assert mode == AuthMode.SMITHERY_STATELESS

    def test_mode_priority_token_exchange_over_basic(self):
        """Test token exchange has priority over BasicAuth."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
            enable_token_exchange=True,
        )

        mode = detect_auth_mode(settings)
        assert mode == AuthMode.OAUTH_TOKEN_EXCHANGE


class TestSingleUserBasicValidation:
    """Test validation for single-user BasicAuth mode."""

    def test_valid_minimal_config(self):
        """Test valid minimal single-user BasicAuth config."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.SINGLE_USER_BASIC
        assert len(errors) == 0

    def test_valid_with_vector_sync(self):
        """Test valid config with vector sync enabled."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
            vector_sync_enabled=True,
            qdrant_location=":memory:",
            ollama_base_url="http://ollama:11434",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.SINGLE_USER_BASIC
        assert len(errors) == 0

    def test_missing_required_host(self):
        """Test error when NEXTCLOUD_HOST is missing."""
        settings = Settings(
            nextcloud_username="admin",
            nextcloud_password="password",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.SINGLE_USER_BASIC
        assert any("nextcloud_host" in err.lower() for err in errors)

    def test_missing_required_username(self):
        """Test that partial credentials fall back to OAuth mode."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_password="password",  # Password without username
        )

        mode, errors = validate_configuration(settings)

        # Mode detection requires BOTH username AND password for single-user BasicAuth
        # If only one is present, it defaults to OAuth single-audience
        assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
        # In OAuth mode, having a password set is forbidden
        assert any("nextcloud_password" in err.lower() for err in errors)

    def test_missing_required_password(self):
        """Test that partial credentials fall back to OAuth mode."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",  # Username without password
        )

        mode, errors = validate_configuration(settings)

        # Mode detection requires BOTH username AND password for single-user BasicAuth
        # If only one is present, it defaults to OAuth single-audience
        assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
        # In OAuth mode, having a username set is forbidden
        assert any("nextcloud_username" in err.lower() for err in errors)

    def test_forbidden_multi_user_basic_auth(self):
        """Test error when ENABLE_MULTI_USER_BASIC_AUTH is set."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
            enable_multi_user_basic_auth=True,
        )

        # Note: This will detect as MULTI_USER_BASIC due to priority
        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.MULTI_USER_BASIC
        # It will fail multi-user validation because username/password are forbidden
        assert len(errors) > 0

    def test_forbidden_token_exchange(self):
        """Test error when ENABLE_TOKEN_EXCHANGE is set."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
            enable_token_exchange=True,
        )

        # Note: This will detect as OAUTH_TOKEN_EXCHANGE due to priority
        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_TOKEN_EXCHANGE
        # It will fail OAuth validation

    def test_vector_sync_without_embedding_provider_uses_fallback(self):
        """Test that vector sync works with Simple provider fallback (no config needed)."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
            vector_sync_enabled=True,
            qdrant_location=":memory:",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.SINGLE_USER_BASIC
        # Should pass - Simple provider is always available as fallback
        assert len(errors) == 0


class TestMultiUserBasicValidation:
    """Test validation for multi-user BasicAuth mode."""

    def test_valid_minimal_config(self):
        """Test valid minimal multi-user BasicAuth config."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_multi_user_basic_auth=True,
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.MULTI_USER_BASIC
        assert len(errors) == 0

    def test_valid_with_offline_access(self):
        """Test valid config with offline access enabled."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_multi_user_basic_auth=True,
            enable_offline_access=True,
            oidc_client_id="test-client",
            oidc_client_secret="test-secret",
            token_encryption_key="test-key-" + "a" * 32,
            token_storage_db="/tmp/tokens.db",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.MULTI_USER_BASIC
        assert len(errors) == 0

    def test_missing_required_host(self):
        """Test error when NEXTCLOUD_HOST is missing."""
        settings = Settings(
            enable_multi_user_basic_auth=True,
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.MULTI_USER_BASIC
        assert any("nextcloud_host" in err.lower() for err in errors)

    def test_forbidden_username_password(self):
        """Test error when NEXTCLOUD_USERNAME/PASSWORD are set."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
            enable_multi_user_basic_auth=True,
        )

        mode, errors = validate_configuration(settings)

        # Multi-user BasicAuth has higher priority than single-user in detection
        # (explicit flags come before credentials)
        assert mode == AuthMode.MULTI_USER_BASIC
        # Should report errors for forbidden username/password
        assert any("nextcloud_username" in err.lower() for err in errors)
        assert any("nextcloud_password" in err.lower() for err in errors)

    def test_offline_access_missing_oauth_credentials(self):
        """Test error when offline access enabled but OAuth credentials missing."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_multi_user_basic_auth=True,
            enable_offline_access=True,
            token_encryption_key="test-key-" + "a" * 32,
            token_storage_db="/tmp/tokens.db",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.MULTI_USER_BASIC
        assert any("oidc_client_id" in err.lower() for err in errors)

    def test_offline_access_missing_encryption_key(self):
        """Test error when offline access enabled but encryption key missing."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_multi_user_basic_auth=True,
            enable_offline_access=True,
            oidc_client_id="test-client",
            oidc_client_secret="test-secret",
            token_storage_db="/tmp/tokens.db",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.MULTI_USER_BASIC
        assert any("token_encryption_key" in err.lower() for err in errors)

    def test_vector_sync_requires_offline_access(self):
        """Test error when vector sync enabled but offline access disabled."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_multi_user_basic_auth=True,
            vector_sync_enabled=True,
            qdrant_location=":memory:",
            ollama_base_url="http://ollama:11434",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.MULTI_USER_BASIC
        assert any("enable_offline_access" in err.lower() for err in errors)


class TestOAuthSingleAudienceValidation:
    """Test validation for OAuth single-audience mode."""

    def test_valid_minimal_config(self):
        """Test valid minimal OAuth single-audience config."""
        settings = Settings(
            nextcloud_host="http://localhost",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
        assert len(errors) == 0

    def test_valid_with_static_credentials(self):
        """Test valid config with static OAuth credentials."""
        settings = Settings(
            nextcloud_host="http://localhost",
            oidc_client_id="test-client",
            oidc_client_secret="test-secret",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
        assert len(errors) == 0

    def test_valid_with_offline_access(self):
        """Test valid config with offline access."""
        settings = Settings(
            nextcloud_host="http://localhost",
            oidc_client_id="test-client",
            oidc_client_secret="test-secret",
            enable_offline_access=True,
            token_encryption_key="test-key-" + "a" * 32,
            token_storage_db="/tmp/tokens.db",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
        assert len(errors) == 0

    def test_forbidden_username_password(self):
        """Test that username/password trigger single-user mode instead."""
        settings = Settings(
            nextcloud_host="http://localhost",
            nextcloud_username="admin",
            nextcloud_password="password",
        )

        mode, errors = validate_configuration(settings)

        # This should detect as SINGLE_USER_BASIC
        assert mode == AuthMode.SINGLE_USER_BASIC

    def test_offline_access_missing_encryption_key(self):
        """Test error when offline access enabled but encryption key missing."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_offline_access=True,
            token_storage_db="/tmp/tokens.db",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
        assert any("token_encryption_key" in err.lower() for err in errors)

    def test_vector_sync_requires_offline_access(self):
        """Test error when vector sync enabled but offline access disabled."""
        settings = Settings(
            nextcloud_host="http://localhost",
            vector_sync_enabled=True,
            qdrant_location=":memory:",
            ollama_base_url="http://ollama:11434",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
        assert any("enable_offline_access" in err.lower() for err in errors)


class TestOAuthTokenExchangeValidation:
    """Test validation for OAuth token exchange mode."""

    def test_valid_minimal_config(self):
        """Test valid minimal OAuth token exchange config."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_token_exchange=True,
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_TOKEN_EXCHANGE
        assert len(errors) == 0

    def test_valid_with_credentials(self):
        """Test valid config with OAuth credentials."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_token_exchange=True,
            oidc_client_id="test-client",
            oidc_client_secret="test-secret",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_TOKEN_EXCHANGE
        assert len(errors) == 0

    def test_forbidden_username_password(self):
        """Test error when username/password are set."""
        settings = Settings(
            nextcloud_host="http://localhost",
            enable_token_exchange=True,
            nextcloud_username="admin",
            nextcloud_password="password",
        )

        mode, errors = validate_configuration(settings)

        assert mode == AuthMode.OAUTH_TOKEN_EXCHANGE
        assert any("nextcloud_username" in err.lower() for err in errors)
        assert any("nextcloud_password" in err.lower() for err in errors)


class TestSmitheryValidation:
    """Test validation for Smithery stateless mode."""

    def test_valid_empty_config(self):
        """Test valid empty config for Smithery mode."""
        settings = Settings()

        with patch.dict(os.environ, {"SMITHERY_DEPLOYMENT": "true"}):
            mode, errors = validate_configuration(settings)

            assert mode == AuthMode.SMITHERY_STATELESS
            assert len(errors) == 0

    def test_forbidden_nextcloud_host(self):
        """Test error when NEXTCLOUD_HOST is set."""
        settings = Settings(
            nextcloud_host="http://localhost",
        )

        with patch.dict(os.environ, {"SMITHERY_DEPLOYMENT": "true"}):
            mode, errors = validate_configuration(settings)

            assert mode == AuthMode.SMITHERY_STATELESS
            assert any("nextcloud_host" in err.lower() for err in errors)

    def test_forbidden_credentials(self):
        """Test error when credentials are set."""
        settings = Settings(
            nextcloud_username="admin",
            nextcloud_password="password",
        )

        with patch.dict(os.environ, {"SMITHERY_DEPLOYMENT": "true"}):
            mode, errors = validate_configuration(settings)

            assert mode == AuthMode.SMITHERY_STATELESS
            assert any("nextcloud_username" in err.lower() for err in errors)

    def test_forbidden_vector_sync(self):
        """Test error when vector sync is enabled."""
        settings = Settings(
            vector_sync_enabled=True,
        )

        with patch.dict(os.environ, {"SMITHERY_DEPLOYMENT": "true"}):
            mode, errors = validate_configuration(settings)

            assert mode == AuthMode.SMITHERY_STATELESS
            assert any("vector_sync_enabled" in err.lower() for err in errors)


class TestModeSummary:
    """Test mode summary generation."""

    def test_single_user_basic_summary(self):
        """Test summary for single-user BasicAuth mode."""
        summary = get_mode_summary(AuthMode.SINGLE_USER_BASIC)

        assert "single_user_basic" in summary
        assert "NEXTCLOUD_HOST" in summary
        assert "NEXTCLOUD_USERNAME" in summary
        assert "NEXTCLOUD_PASSWORD" in summary
        assert "VECTOR_SYNC_ENABLED" in summary

    def test_smithery_summary(self):
        """Test summary for Smithery mode."""
        summary = get_mode_summary(AuthMode.SMITHERY_STATELESS)

        assert "smithery" in summary
        assert "session" in summary.lower()
        assert "(none" in summary  # No required config

    def test_oauth_token_exchange_summary(self):
        """Test summary for OAuth token exchange mode."""
        summary = get_mode_summary(AuthMode.OAUTH_TOKEN_EXCHANGE)

        assert "oauth_exchange" in summary
        assert "ENABLE_TOKEN_EXCHANGE" in summary
        assert "RFC 8693" in summary


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_string_treated_as_missing(self):
        """Test that empty strings are treated as missing values."""
        settings = Settings(
            nextcloud_host="",  # Empty string
            nextcloud_username="admin",
            nextcloud_password="password",
        )

        mode, errors = validate_configuration(settings)

        # Should fail because nextcloud_host is effectively missing
        assert any("nextcloud_host" in err.lower() for err in errors)

    def test_whitespace_treated_as_missing(self):
        """Test that whitespace-only strings are treated as missing."""
        settings = Settings(
            nextcloud_host="   ",  # Whitespace only
            nextcloud_username="admin",
            nextcloud_password="password",
        )

        mode, errors = validate_configuration(settings)

        # Should fail because nextcloud_host is effectively missing
        assert any("nextcloud_host" in err.lower() for err in errors)

    def test_multiple_errors_reported(self):
        """Test that multiple errors are all reported."""
        settings = Settings(
            # Missing all required fields for single-user BasicAuth
        )

        mode, errors = validate_configuration(settings)

        # Should have errors for missing host (OAuth mode is default)
        assert len(errors) > 0
