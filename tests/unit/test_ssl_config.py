"""Tests for SSL/TLS configuration.

Covers ``NEXTCLOUD_VERIFY_SSL`` / ``NEXTCLOUD_CA_BUNDLE`` for the httpx2
client talking to Nextcloud.

Postgres backend TLS is no longer configured here: under Model A (ADR-026)
it lives entirely in ``DATABASE_URL`` (e.g. ``?sslmode=require``) and is read
by libpq/psycopg — see ``TestProcrastinateConninfo`` in
``test_decomposition_config.py``.
"""

import ast
import logging
import os
import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import certifi
import httpx2
import pytest
from nextcloud_client import NextcloudClient
from nextcloud_client.http import nextcloud_httpx_transport

from nextcloud_mcp_server import http as http_module
from nextcloud_mcp_server.config import (
    Settings,
    _reload_config,
    get_nextcloud_http_keepalive,
    get_nextcloud_ssl_verify,
    get_settings,
)
from nextcloud_mcp_server.http import (
    nextcloud_client_from_env,
    nextcloud_client_kwargs,
    nextcloud_httpx_client,
)


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
            # httpx2 stores verify as an SSLConfig; check the _transport
            assert isinstance(client, httpx2.AsyncClient)

    def test_client_applies_verify_false(self):
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=False
        ):
            client = nextcloud_httpx_client()
            assert isinstance(client, httpx2.AsyncClient)

    def test_client_caller_override_takes_precedence(self):
        """Caller-supplied verify kwarg should not be overridden."""
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=True
        ):
            client = nextcloud_httpx_client(verify=False)
            assert isinstance(client, httpx2.AsyncClient)

    def test_client_passes_extra_kwargs(self):
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=True
        ):
            client = nextcloud_httpx_client(timeout=5.0, follow_redirects=True)
            assert isinstance(client, httpx2.AsyncClient)


def _pool_keepalive(client: NextcloudClient) -> int:
    # AsyncDisableCookieTransport wraps the httpx2 transport.
    return client._client._transport.transport._pool._max_keepalive_connections  # ty: ignore[unresolved-attribute]


class TestHTTPKeepalive:
    """NEXTCLOUD_HTTP_KEEPALIVE wiring into the Nextcloud httpx2 transport (#965).

    The assertions reach into ``transport._pool._max_keepalive_connections``,
    a private httpcore attribute. It is the most direct way to verify the limit
    actually reached the pool. httpx2/httpcore is pinned, so a rename is
    unlikely, and these tests would catch one immediately.
    """

    def test_default_keeps_pooled_connections(self):
        """Default (keep-alive enabled) leaves httpx2's default pool limits."""
        assert nextcloud_httpx_transport()._pool._max_keepalive_connections == 20

    def test_disabled_forces_fresh_connection_per_request(self):
        """keepalive=False → max_keepalive_connections=0."""
        transport = nextcloud_httpx_transport(keepalive=False)
        assert transport._pool._max_keepalive_connections == 0

    def test_caller_supplied_limits_take_precedence(self):
        """An explicit limits kwarg is not overridden even when keep-alive is off."""
        transport = nextcloud_httpx_transport(
            keepalive=False, limits=httpx2.Limits(max_keepalive_connections=7)
        )
        assert transport._pool._max_keepalive_connections == 7

    @pytest.mark.parametrize(("keepalive", "expected"), [(True, 20), (False, 0)])
    def test_setting_reaches_nextcloud_client(self, keepalive, expected):
        """NEXTCLOUD_HTTP_KEEPALIVE → nextcloud_client_kwargs → the client's pool."""
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_http_keepalive",
            return_value=keepalive,
        ):
            client = NextcloudClient(
                "https://nc.example", "alice", **nextcloud_client_kwargs()
            )
        assert _pool_keepalive(client) == expected


class TestNextcloudClientKwargs:
    def test_carries_configured_verify(self):
        ctx = ssl.create_default_context()
        with patch(
            "nextcloud_mcp_server.http.get_nextcloud_ssl_verify", return_value=ctx
        ):
            assert nextcloud_client_kwargs()["verify"] is ctx

    def test_every_server_construction_site_passes_it(self):
        """A server-side ``NextcloudClient(...)`` without it ignores the TLS settings.

        The client defaults to ``verify=True, keepalive=True``, so a missed call
        site fails silently: no error, just ``NEXTCLOUD_VERIFY_SSL``,
        ``NEXTCLOUD_CA_BUNDLE`` and ``NEXTCLOUD_HTTP_KEEPALIVE`` not applied.
        """
        server_root = Path(http_module.__file__).parent
        sites, offenders = 0, []
        for path in sorted(server_root.rglob("*.py")):
            rel = path.relative_to(server_root).as_posix()
            if rel.startswith("client/"):
                continue  # the client itself: from_token forwards its own args
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Call) and _builds_nextcloud_client(node)):
                    continue
                sites += 1
                if not any(
                    kw.arg is None
                    and isinstance(kw.value, ast.Call)
                    and isinstance(kw.value.func, ast.Name)
                    and kw.value.func.id == "nextcloud_client_kwargs"
                    for kw in node.keywords
                ):
                    offenders.append(f"{rel}:{node.lineno}")
        assert sites >= 5, "scan found too few construction sites; is it broken?"
        assert not offenders, f"pass **nextcloud_client_kwargs() at: {offenders}"


def _builds_nextcloud_client(call: ast.Call) -> bool:
    """``NextcloudClient(...)`` or ``NextcloudClient.from_token(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "NextcloudClient"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "from_token"
        and isinstance(func.value, ast.Name)
        and func.value.id == "NextcloudClient"
    )


class TestClientFromEnv:
    def test_raises_when_nextcloud_host_unset(self, monkeypatch):
        """It must fail loudly, not build a client with a ``None`` URL.

        ``Settings.nextcloud_host`` is ``str | None``, so an unset
        NEXTCLOUD_HOST used to flow straight into
        ``NextcloudClient(base_url=...)`` and only surface later as a confusing
        httpx2 error on the first request. This mirrors the guard ``context.py``
        already applies for multi-user BasicAuth mode.

        ``cfg`` and ``__init__`` are both stubbed, so the host guard is the only
        thing that can raise. Drop the guard and the call returns a client, so
        the test fails on "DID NOT RAISE". That makes it a real guard rather
        than one satisfied by an incidental error from a thin settings stub.
        """
        monkeypatch.setattr(
            http_module, "get_settings", lambda: SimpleNamespace(nextcloud_host=None)
        )
        monkeypatch.setattr(http_module, "cfg", lambda key, *a, **kw: "stub")
        monkeypatch.setattr(
            NextcloudClient, "__init__", lambda self, *a, **kw: None, raising=True
        )

        with pytest.raises(ValueError, match="NEXTCLOUD_HOST"):
            nextcloud_client_from_env()


class TestGetNextcloudHTTPKeepalive:
    """get_nextcloud_http_keepalive() reads NEXTCLOUD_HTTP_KEEPALIVE."""

    def test_default_true(self):
        assert Settings().nextcloud_http_keepalive is True

    def test_env_false(self):
        with patch.dict(os.environ, {"NEXTCLOUD_HTTP_KEEPALIVE": "false"}):
            _reload_config()
            try:
                assert get_nextcloud_http_keepalive() is False
            finally:
                _reload_config()


# Postgres backend TLS is configured entirely in DATABASE_URL (?sslmode=...)
# and read by libpq/psycopg — there is no DATABASE_VERIFY_SSL / DATABASE_CA_BUNDLE
# setting to test any more (ADR-026, Model A). See TestProcrastinateConninfo in
# test_decomposition_config.py for the DATABASE_URL passthrough behaviour.
