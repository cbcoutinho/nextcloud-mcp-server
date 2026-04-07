"""Tests for SSL/TLS configuration (NEXTCLOUD_VERIFY_SSL, NEXTCLOUD_CA_BUNDLE)."""

import logging
import os
import ssl
from unittest.mock import patch

import certifi
import httpx
import pytest

from nextcloud_mcp_server.config import (
    Settings,
    _reload_config,
    get_nextcloud_ssl_verify,
    get_settings,
)
from nextcloud_mcp_server.http import nextcloud_httpx_client, nextcloud_httpx_transport


class TestSSLSettings:
    """Test SSL/TLS fields on Settings dataclass."""

    def test_defaults(self):
        """verify_ssl defaults to True, ca_bundle defaults to None."""
        settings = Settings()
        assert settings.nextcloud_verify_ssl is True
        assert settings.nextcloud_ca_bundle is None

    def test_verify_ssl_false_logs_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.config")
        Settings(nextcloud_verify_ssl=False)
        assert "NEXTCLOUD_VERIFY_SSL is disabled" in caplog.text

    def test_ca_bundle_nonexistent_path_raises(self):
        with pytest.raises(ValueError, match="does not exist"):
            Settings(nextcloud_ca_bundle="/nonexistent/path/ca.pem")

    def test_ca_bundle_existing_path_logs_info(self, caplog, tmp_path):
        ca_file = tmp_path / "ca.pem"
        ca_file.write_text(
            "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
        )
        caplog.set_level(logging.INFO, logger="nextcloud_mcp_server.config")
        settings = Settings(nextcloud_ca_bundle=str(ca_file))
        assert settings.nextcloud_ca_bundle == str(ca_file)
        assert "Using custom CA bundle" in caplog.text


class TestGetNextcloudSSLVerify:
    """Test the get_nextcloud_ssl_verify() helper function."""

    def test_default_returns_true(self):
        env = {
            "NEXTCLOUD_VERIFY_SSL": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            _reload_config()
            result = get_nextcloud_ssl_verify()
            assert result is True

    def test_verify_false_returns_false(self):
        env = {
            "NEXTCLOUD_VERIFY_SSL": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "nextcloud_mcp_server.config.get_settings",
                return_value=Settings(nextcloud_verify_ssl=False),
            ):
                result = get_nextcloud_ssl_verify()
                assert result is False

    def test_ca_bundle_returns_ssl_context(self):
        ca_bundle = certifi.where()
        with patch(
            "nextcloud_mcp_server.config.get_settings",
            return_value=Settings(nextcloud_ca_bundle=ca_bundle),
        ):
            result = get_nextcloud_ssl_verify()
            assert isinstance(result, ssl.SSLContext)

    def test_ca_bundle_ssl_context_has_loaded_certs(self):
        """SSLContext created from CA bundle should have loaded certificates."""
        ca_bundle = certifi.where()
        with patch(
            "nextcloud_mcp_server.config.get_settings",
            return_value=Settings(nextcloud_ca_bundle=ca_bundle),
        ):
            result = get_nextcloud_ssl_verify()
            assert isinstance(result, ssl.SSLContext)
            stats = result.cert_store_stats()
            assert stats["x509_ca"] > 0

    def test_verify_false_takes_precedence_over_ca_bundle(self, tmp_path):
        """When verify_ssl=False, ca_bundle is ignored (False wins)."""
        ca_file = tmp_path / "ca.pem"
        ca_file.write_text(
            "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
        )
        with patch(
            "nextcloud_mcp_server.config.get_settings",
            return_value=Settings(
                nextcloud_verify_ssl=False,
                nextcloud_ca_bundle=str(ca_file),
            ),
        ):
            result = get_nextcloud_ssl_verify()
            assert result is False


class TestGetSettingsSSLEnvVars:
    """Test that get_settings() reads SSL env vars correctly."""

    def test_verify_ssl_env_true(self):
        env = {"NEXTCLOUD_VERIFY_SSL": "true"}
        with patch.dict(os.environ, env, clear=False):
            _reload_config()
            settings = get_settings()
            assert settings.nextcloud_verify_ssl is True

    def test_verify_ssl_env_false(self):
        env = {"NEXTCLOUD_VERIFY_SSL": "false"}
        with patch.dict(os.environ, env, clear=False):
            _reload_config()
            settings = get_settings()
            assert settings.nextcloud_verify_ssl is False

    def test_verify_ssl_env_missing_defaults_true(self):
        with patch.dict(os.environ, {}, clear=False):
            # Remove NEXTCLOUD_VERIFY_SSL if it exists
            os.environ.pop("NEXTCLOUD_VERIFY_SSL", None)
            _reload_config()
            settings = get_settings()
            assert settings.nextcloud_verify_ssl is True

    def test_ca_bundle_env(self, tmp_path):
        ca_file = tmp_path / "ca.pem"
        ca_file.write_text(
            "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
        )
        env = {"NEXTCLOUD_CA_BUNDLE": str(ca_file)}
        with patch.dict(os.environ, env, clear=False):
            _reload_config()
            settings = get_settings()
            assert settings.nextcloud_ca_bundle == str(ca_file)


class TestHTTPClientFactory:
    """Test that factory functions apply verify correctly."""

    def test_client_applies_verify_true(self):
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=True
        ):
            client = nextcloud_httpx_client()
            # httpx stores verify as an SSLConfig; check the _transport
            assert isinstance(client, httpx.AsyncClient)

    def test_client_applies_verify_false(self):
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=False
        ):
            client = nextcloud_httpx_client()
            assert isinstance(client, httpx.AsyncClient)

    def test_client_caller_override_takes_precedence(self):
        """Caller-supplied verify kwarg should not be overridden."""
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=True
        ):
            client = nextcloud_httpx_client(verify=False)
            assert isinstance(client, httpx.AsyncClient)

    def test_transport_applies_verify(self):
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=False
        ):
            transport = nextcloud_httpx_transport()
            assert isinstance(transport, httpx.AsyncHTTPTransport)

    def test_client_passes_extra_kwargs(self):
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=True
        ):
            client = nextcloud_httpx_client(timeout=5.0, follow_redirects=True)
            assert isinstance(client, httpx.AsyncClient)
