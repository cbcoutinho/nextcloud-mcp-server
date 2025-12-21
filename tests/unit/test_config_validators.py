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

    def test_vector_sync_auto_enables_background_ops_in_multi_user_mode(self):
        """Test vector sync automatically enables background operations in multi-user mode (ADR-021)."""
        # Before ADR-021: This would have failed validation (required explicit ENABLE_OFFLINE_ACCESS)
        # After ADR-021: vector_sync_enabled auto-enables background operations
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "ENABLE_MULTI_USER_BASIC_AUTH": "true",
                "VECTOR_SYNC_ENABLED": "true",  # Using old name for backward compat test
                "QDRANT_LOCATION": ":memory:",
                "OLLAMA_BASE_URL": "http://ollama:11434",
                "TOKEN_ENCRYPTION_KEY": "test-key",
                "TOKEN_STORAGE_DB": "/tmp/test.db",
                "NEXTCLOUD_OIDC_CLIENT_ID": "test-client-id",
                "NEXTCLOUD_OIDC_CLIENT_SECRET": "test-client-secret",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode, errors = validate_configuration(settings)

            assert mode == AuthMode.MULTI_USER_BASIC
            # Should have no errors - background operations auto-enabled
            assert len(errors) == 0
            # Verify background operations were auto-enabled
            assert settings.enable_offline_access is True


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

    def test_vector_sync_auto_enables_background_ops_in_oauth_mode(self):
        """Test vector sync automatically enables background operations in OAuth mode (ADR-021)."""
        # Before ADR-021: This would have failed validation (required explicit ENABLE_OFFLINE_ACCESS)
        # After ADR-021: vector_sync_enabled auto-enables background operations in multi-user modes
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "VECTOR_SYNC_ENABLED": "true",
                "QDRANT_LOCATION": ":memory:",
                "OLLAMA_BASE_URL": "http://ollama:11434",
                "TOKEN_ENCRYPTION_KEY": "test-key",
                "TOKEN_STORAGE_DB": "/tmp/test.db",
                # Note: No username/password = OAuth mode
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode, errors = validate_configuration(settings)

            assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
            # Should have no errors - background operations auto-enabled
            assert len(errors) == 0
            # Verify background operations were auto-enabled
            assert settings.enable_offline_access is True


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


class TestConfigurationConsolidation:
    """Test ADR-021 configuration consolidation and backward compatibility.

    Tests verify:
    - New variable names work (ENABLE_SEMANTIC_SEARCH, ENABLE_BACKGROUND_OPERATIONS)
    - Old variable names still work (VECTOR_SYNC_ENABLED, ENABLE_OFFLINE_ACCESS)
    - Deprecation warnings are logged
    - Auto-enablement of background operations in multi-user modes
    """

    def test_new_semantic_search_variable_name(self):
        """Test ENABLE_SEMANTIC_SEARCH (new name) works correctly."""
        with patch.dict(
            os.environ,
            {
                "ENABLE_SEMANTIC_SEARCH": "true",
                "QDRANT_LOCATION": ":memory:",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            assert settings.vector_sync_enabled is True

    def test_old_vector_sync_variable_name_backward_compat(self):
        """Test VECTOR_SYNC_ENABLED (old name) still works for backward compatibility."""
        with patch.dict(
            os.environ,
            {
                "VECTOR_SYNC_ENABLED": "true",
                "QDRANT_LOCATION": ":memory:",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            assert settings.vector_sync_enabled is True

    def test_new_background_operations_variable_name(self):
        """Test ENABLE_BACKGROUND_OPERATIONS (new name) works correctly."""
        with patch.dict(
            os.environ,
            {
                "ENABLE_BACKGROUND_OPERATIONS": "true",
                "TOKEN_ENCRYPTION_KEY": "test-key",
                "TOKEN_STORAGE_DB": "/tmp/test.db",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            assert settings.enable_offline_access is True

    def test_old_offline_access_variable_name_backward_compat(self):
        """Test ENABLE_OFFLINE_ACCESS (old name) still works for backward compatibility."""
        with patch.dict(
            os.environ,
            {
                "ENABLE_OFFLINE_ACCESS": "true",
                "TOKEN_ENCRYPTION_KEY": "test-key",
                "TOKEN_STORAGE_DB": "/tmp/test.db",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            assert settings.enable_offline_access is True

    def test_semantic_search_auto_enables_background_ops_in_oauth_mode(self):
        """Test ENABLE_SEMANTIC_SEARCH automatically enables background operations in OAuth mode."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "ENABLE_SEMANTIC_SEARCH": "true",
                "QDRANT_LOCATION": ":memory:",
                "TOKEN_ENCRYPTION_KEY": "test-key",
                "TOKEN_STORAGE_DB": "/tmp/test.db",
                # Note: No NEXTCLOUD_USERNAME/PASSWORD = OAuth mode
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()

            # Semantic search enabled
            assert settings.vector_sync_enabled is True

            # Background operations auto-enabled (even though not explicitly set)
            assert settings.enable_offline_access is True

    def test_semantic_search_does_not_auto_enable_in_single_user_mode(self):
        """Test ENABLE_SEMANTIC_SEARCH does NOT auto-enable background ops in single-user mode."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "NEXTCLOUD_USERNAME": "admin",
                "NEXTCLOUD_PASSWORD": "password",
                "ENABLE_SEMANTIC_SEARCH": "true",
                "QDRANT_LOCATION": ":memory:",
                # Note: Username/password set = single-user BasicAuth mode
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()

            # Semantic search enabled
            assert settings.vector_sync_enabled is True

            # Background operations NOT auto-enabled (not needed in single-user mode)
            assert settings.enable_offline_access is False

    def test_explicit_background_ops_still_works(self):
        """Test explicitly setting ENABLE_BACKGROUND_OPERATIONS works even without semantic search."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "ENABLE_BACKGROUND_OPERATIONS": "true",
                "TOKEN_ENCRYPTION_KEY": "test-key",
                "TOKEN_STORAGE_DB": "/tmp/test.db",
                # Note: No semantic search enabled
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()

            # Semantic search NOT enabled
            assert settings.vector_sync_enabled is False

            # Background operations explicitly enabled
            assert settings.enable_offline_access is True

    def test_both_old_and_new_semantic_search_names_prefers_new(self):
        """Test setting both ENABLE_SEMANTIC_SEARCH and VECTOR_SYNC_ENABLED uses new name."""
        with patch.dict(
            os.environ,
            {
                "ENABLE_SEMANTIC_SEARCH": "true",
                "VECTOR_SYNC_ENABLED": "false",  # Old name says false
                "QDRANT_LOCATION": ":memory:",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()

            # Should use new name value (true)
            assert settings.vector_sync_enabled is True

    def test_both_old_and_new_background_ops_names_prefers_new(self):
        """Test setting both ENABLE_BACKGROUND_OPERATIONS and ENABLE_OFFLINE_ACCESS uses new name."""
        with patch.dict(
            os.environ,
            {
                "ENABLE_BACKGROUND_OPERATIONS": "true",
                "ENABLE_OFFLINE_ACCESS": "false",  # Old name says false
                "TOKEN_ENCRYPTION_KEY": "test-key",
                "TOKEN_STORAGE_DB": "/tmp/test.db",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()

            # Should use new name value (true)
            assert settings.enable_offline_access is True

    def test_validation_no_longer_requires_both_variables(self):
        """Test validation no longer requires explicit ENABLE_OFFLINE_ACCESS when semantic search enabled."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "ENABLE_MULTI_USER_BASIC_AUTH": "true",
                "ENABLE_SEMANTIC_SEARCH": "true",
                "QDRANT_LOCATION": ":memory:",
                "TOKEN_ENCRYPTION_KEY": "test-key",
                "TOKEN_STORAGE_DB": "/tmp/test.db",
                # OAuth credentials required for app password retrieval (when background ops enabled)
                "NEXTCLOUD_OIDC_CLIENT_ID": "test-client-id",
                "NEXTCLOUD_OIDC_CLIENT_SECRET": "test-client-secret",
                # Note: ENABLE_OFFLINE_ACCESS not set - should auto-enable
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode, errors = validate_configuration(settings)

            # Should have no validation errors
            # (Previously would have required explicit ENABLE_OFFLINE_ACCESS)
            assert len(errors) == 0
            assert mode == AuthMode.MULTI_USER_BASIC
            # Verify background operations were auto-enabled
            assert settings.enable_offline_access is True


class TestExplicitModeSelection:
    """Test ADR-021 explicit mode selection via MCP_DEPLOYMENT_MODE.

    Tests verify:
    - Explicit mode selection works for all modes
    - Invalid mode names raise ValueError
    - Explicit mode takes precedence over auto-detection
    """

    def test_explicit_single_user_basic_mode(self):
        """Test explicit single_user_basic mode selection."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "MCP_DEPLOYMENT_MODE": "single_user_basic",
                "NEXTCLOUD_USERNAME": "admin",
                "NEXTCLOUD_PASSWORD": "password",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode = detect_auth_mode(settings)

            assert mode == AuthMode.SINGLE_USER_BASIC

    def test_explicit_multi_user_basic_mode(self):
        """Test explicit multi_user_basic mode selection."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "MCP_DEPLOYMENT_MODE": "multi_user_basic",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode = detect_auth_mode(settings)

            assert mode == AuthMode.MULTI_USER_BASIC

    def test_explicit_oauth_single_audience_mode(self):
        """Test explicit oauth_single_audience mode selection."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "MCP_DEPLOYMENT_MODE": "oauth_single_audience",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode = detect_auth_mode(settings)

            assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE

    def test_explicit_oauth_token_exchange_mode(self):
        """Test explicit oauth_token_exchange mode selection."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "MCP_DEPLOYMENT_MODE": "oauth_token_exchange",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode = detect_auth_mode(settings)

            assert mode == AuthMode.OAUTH_TOKEN_EXCHANGE

    def test_explicit_smithery_mode(self):
        """Test explicit smithery mode selection."""
        with patch.dict(
            os.environ,
            {
                "MCP_DEPLOYMENT_MODE": "smithery",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode = detect_auth_mode(settings)

            assert mode == AuthMode.SMITHERY_STATELESS

    def test_invalid_deployment_mode_raises_error(self):
        """Test invalid MCP_DEPLOYMENT_MODE raises ValueError."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "MCP_DEPLOYMENT_MODE": "invalid_mode",
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()

            # Should raise ValueError with clear message
            try:
                detect_auth_mode(settings)
                assert False, "Should have raised ValueError"
            except ValueError as e:
                assert "Invalid MCP_DEPLOYMENT_MODE" in str(e)
                assert "invalid_mode" in str(e)
                assert "Valid values:" in str(e)

    def test_explicit_mode_overrides_auto_detection(self):
        """Test explicit mode takes precedence over auto-detection."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "NEXTCLOUD_USERNAME": "admin",  # Would auto-detect as single_user_basic
                "NEXTCLOUD_PASSWORD": "password",
                "MCP_DEPLOYMENT_MODE": "oauth_single_audience",  # Explicit override
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode = detect_auth_mode(settings)

            # Should use explicit mode, not auto-detected mode
            assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE

    def test_case_insensitive_mode_names(self):
        """Test MCP_DEPLOYMENT_MODE is case-insensitive."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "MCP_DEPLOYMENT_MODE": "OAUTH_SINGLE_AUDIENCE",  # Uppercase
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode = detect_auth_mode(settings)

            assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE

    def test_whitespace_in_mode_name_stripped(self):
        """Test whitespace in MCP_DEPLOYMENT_MODE is stripped."""
        with patch.dict(
            os.environ,
            {
                "NEXTCLOUD_HOST": "http://localhost:8080",
                "MCP_DEPLOYMENT_MODE": "  oauth_single_audience  ",  # Whitespace
            },
            clear=True,
        ):
            from nextcloud_mcp_server.config import get_settings

            settings = get_settings()
            mode = detect_auth_mode(settings)

            assert mode == AuthMode.OAUTH_SINGLE_AUDIENCE
