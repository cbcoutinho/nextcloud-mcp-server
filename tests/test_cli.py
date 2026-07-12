"""Tests for CLI options using Click's testing utilities."""

import os
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nextcloud_mcp_server.cli import _init_worker_observability, run, worker


@pytest.fixture
def runner():
    """Create a Click CLI runner."""
    return CliRunner()


@pytest.fixture
def clean_env(monkeypatch):
    """Clean environment variables before each test."""
    env_vars = [
        "NEXTCLOUD_HOST",
        "NEXTCLOUD_USERNAME",
        "NEXTCLOUD_PASSWORD",
        "NEXTCLOUD_OIDC_CLIENT_ID",
        "NEXTCLOUD_OIDC_CLIENT_SECRET",
        "NEXTCLOUD_OIDC_SCOPES",
        "NEXTCLOUD_OIDC_TOKEN_TYPE",
        "NEXTCLOUD_MCP_SERVER_URL",
        "NEXTCLOUD_PUBLIC_ISSUER_URL",
    ]
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)


def test_help_message_displays_all_options(runner):
    """Test that help message includes all new CLI options."""
    result = runner.invoke(run, ["--help"])
    assert result.exit_code == 0

    # Check for new options
    assert "--nextcloud-host" in result.output
    assert "--nextcloud-username" in result.output
    assert "--nextcloud-password" in result.output
    assert "--oauth-scopes" in result.output
    assert "--oauth-token-type" in result.output
    assert "--public-issuer-url" in result.output

    # Check for existing options
    assert "--oauth-client-id" in result.output
    assert "--oauth-client-secret" in result.output
    assert "--mcp-server-url" in result.output


def test_token_type_accepts_valid_values(runner, clean_env):
    """Test that --oauth-token-type accepts bearer and jwt (case insensitive)."""
    # Test lowercase bearer
    result = runner.invoke(run, ["--oauth-token-type", "bearer", "--help"])
    assert result.exit_code == 0

    # Test lowercase jwt
    result = runner.invoke(run, ["--oauth-token-type", "jwt", "--help"])
    assert result.exit_code == 0

    # Test uppercase (should work with case_sensitive=False)
    result = runner.invoke(run, ["--oauth-token-type", "Bearer", "--help"])
    assert result.exit_code == 0

    result = runner.invoke(run, ["--oauth-token-type", "JWT", "--help"])
    assert result.exit_code == 0


def test_token_type_rejects_invalid_values(runner, clean_env):
    """Test that --oauth-token-type rejects invalid values."""
    result = runner.invoke(run, ["--oauth-token-type", "invalid"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output


def test_cli_options_set_environment_variables(runner, clean_env, monkeypatch):
    """Test that CLI options set environment variables correctly."""
    # We need to mock the actual server startup to avoid connection errors
    # Store the env vars that get set
    captured_env = {}

    def mock_get_app(*args, **kwargs):
        # Capture environment variables after they're set by CLI
        captured_env.update(
            {
                "NEXTCLOUD_HOST": os.environ.get("NEXTCLOUD_HOST"),
                "NEXTCLOUD_USERNAME": os.environ.get("NEXTCLOUD_USERNAME"),
                "NEXTCLOUD_PASSWORD": os.environ.get("NEXTCLOUD_PASSWORD"),
                "NEXTCLOUD_OIDC_SCOPES": os.environ.get("NEXTCLOUD_OIDC_SCOPES"),
                "NEXTCLOUD_OIDC_TOKEN_TYPE": os.environ.get(
                    "NEXTCLOUD_OIDC_TOKEN_TYPE"
                ),
                "NEXTCLOUD_PUBLIC_ISSUER_URL": os.environ.get(
                    "NEXTCLOUD_PUBLIC_ISSUER_URL"
                ),
                "NEXTCLOUD_MCP_SERVER_URL": os.environ.get("NEXTCLOUD_MCP_SERVER_URL"),
            }
        )
        # Raise an exception to stop execution before uvicorn.run
        raise SystemExit(0)

    # Patch get_app to capture env vars
    monkeypatch.setattr("nextcloud_mcp_server.cli.get_app", mock_get_app)

    _ = runner.invoke(
        run,
        [
            "--nextcloud-host",
            "https://test.example.com",
            "--nextcloud-username",
            "testuser",
            "--nextcloud-password",
            "testpass",
            "--oauth-scopes",
            "openid nc:read",
            "--oauth-token-type",
            "jwt",
            "--public-issuer-url",
            "https://public.example.com",
            "--mcp-server-url",
            "http://test:8000",
        ],
    )

    # Verify environment variables were set
    assert captured_env["NEXTCLOUD_HOST"] == "https://test.example.com"
    assert captured_env["NEXTCLOUD_USERNAME"] == "testuser"
    assert captured_env["NEXTCLOUD_PASSWORD"] == "testpass"
    assert captured_env["NEXTCLOUD_OIDC_SCOPES"] == "openid nc:read"
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] == "jwt"
    assert captured_env["NEXTCLOUD_PUBLIC_ISSUER_URL"] == "https://public.example.com"
    assert captured_env["NEXTCLOUD_MCP_SERVER_URL"] == "http://test:8000"


def test_cli_options_override_environment_variables(runner, monkeypatch):
    """Test that CLI options override environment variables."""
    # Set environment variables
    monkeypatch.setenv("NEXTCLOUD_HOST", "https://from-env.example.com")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "envuser")
    monkeypatch.setenv("NEXTCLOUD_OIDC_SCOPES", "openid")
    monkeypatch.setenv("NEXTCLOUD_OIDC_TOKEN_TYPE", "bearer")

    captured_env = {}

    def mock_get_app(*args, **kwargs):
        captured_env.update(
            {
                "NEXTCLOUD_HOST": os.environ.get("NEXTCLOUD_HOST"),
                "NEXTCLOUD_USERNAME": os.environ.get("NEXTCLOUD_USERNAME"),
                "NEXTCLOUD_OIDC_SCOPES": os.environ.get("NEXTCLOUD_OIDC_SCOPES"),
                "NEXTCLOUD_OIDC_TOKEN_TYPE": os.environ.get(
                    "NEXTCLOUD_OIDC_TOKEN_TYPE"
                ),
            }
        )
        raise SystemExit(0)

    monkeypatch.setattr("nextcloud_mcp_server.cli.get_app", mock_get_app)

    # Provide CLI options that should override env vars
    _ = runner.invoke(
        run,
        [
            "--nextcloud-host",
            "https://from-cli.example.com",
            "--nextcloud-username",
            "cliuser",
            "--oauth-scopes",
            "openid nc:write",
            "--oauth-token-type",
            "jwt",
        ],
    )

    # Verify CLI options overrode env vars
    assert captured_env["NEXTCLOUD_HOST"] == "https://from-cli.example.com"
    assert captured_env["NEXTCLOUD_USERNAME"] == "cliuser"
    assert captured_env["NEXTCLOUD_OIDC_SCOPES"] == "openid nc:write"
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] == "jwt"


def test_environment_variables_used_when_cli_not_provided(runner, monkeypatch):
    """Test that environment variables are used when CLI options not provided."""
    # Set environment variables
    monkeypatch.setenv("NEXTCLOUD_HOST", "https://from-env.example.com")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "envuser")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "envpass")
    monkeypatch.setenv("NEXTCLOUD_OIDC_SCOPES", "openid email")
    monkeypatch.setenv("NEXTCLOUD_OIDC_TOKEN_TYPE", "jwt")
    monkeypatch.setenv("NEXTCLOUD_PUBLIC_ISSUER_URL", "https://public-env.example.com")

    captured_env = {}

    def mock_get_app(*args, **kwargs):
        captured_env.update(
            {
                "NEXTCLOUD_HOST": os.environ.get("NEXTCLOUD_HOST"),
                "NEXTCLOUD_USERNAME": os.environ.get("NEXTCLOUD_USERNAME"),
                "NEXTCLOUD_PASSWORD": os.environ.get("NEXTCLOUD_PASSWORD"),
                "NEXTCLOUD_OIDC_SCOPES": os.environ.get("NEXTCLOUD_OIDC_SCOPES"),
                "NEXTCLOUD_OIDC_TOKEN_TYPE": os.environ.get(
                    "NEXTCLOUD_OIDC_TOKEN_TYPE"
                ),
                "NEXTCLOUD_PUBLIC_ISSUER_URL": os.environ.get(
                    "NEXTCLOUD_PUBLIC_ISSUER_URL"
                ),
            }
        )
        raise SystemExit(0)

    monkeypatch.setattr("nextcloud_mcp_server.cli.get_app", mock_get_app)

    # Don't provide any CLI options - should use env vars
    _ = runner.invoke(run, [])

    # Verify env vars were used
    assert captured_env["NEXTCLOUD_HOST"] == "https://from-env.example.com"
    assert captured_env["NEXTCLOUD_USERNAME"] == "envuser"
    assert captured_env["NEXTCLOUD_PASSWORD"] == "envpass"
    assert captured_env["NEXTCLOUD_OIDC_SCOPES"] == "openid email"
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] == "jwt"
    assert (
        captured_env["NEXTCLOUD_PUBLIC_ISSUER_URL"] == "https://public-env.example.com"
    )


def test_default_values(runner, clean_env, monkeypatch):
    """Test that default values are used when neither CLI nor env vars provided."""
    captured_env = {}

    def mock_get_app(*args, **kwargs):
        captured_env.update(
            {
                "NEXTCLOUD_OIDC_SCOPES": os.environ.get("NEXTCLOUD_OIDC_SCOPES"),
                "NEXTCLOUD_OIDC_TOKEN_TYPE": os.environ.get(
                    "NEXTCLOUD_OIDC_TOKEN_TYPE"
                ),
                "NEXTCLOUD_MCP_SERVER_URL": os.environ.get("NEXTCLOUD_MCP_SERVER_URL"),
            }
        )
        raise SystemExit(0)

    monkeypatch.setattr("nextcloud_mcp_server.cli.get_app", mock_get_app)

    # Don't provide CLI options or env vars - should use defaults
    _ = runner.invoke(run, [])

    # Verify default values
    assert captured_env["NEXTCLOUD_OIDC_SCOPES"] == (
        "openid profile email "
        "notes.read notes.write "
        "calendar.read calendar.write "
        "todo.read todo.write "
        "contacts.read contacts.write "
        "cookbook.read cookbook.write "
        "deck.read deck.write "
        "tables.read tables.write "
        "files.read files.write "
        "sharing.read sharing.write"
    )
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] == "bearer"
    assert captured_env["NEXTCLOUD_MCP_SERVER_URL"] == "http://localhost:8000"


def test_oauth_token_type_case_normalization(runner, clean_env, monkeypatch):
    """Test that token type is normalized correctly regardless of input case."""
    captured_env = {}

    def mock_get_app(*args, **kwargs):
        captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] = os.environ.get(
            "NEXTCLOUD_OIDC_TOKEN_TYPE"
        )
        raise SystemExit(0)

    monkeypatch.setattr("nextcloud_mcp_server.cli.get_app", mock_get_app)

    # Test uppercase JWT
    runner.invoke(run, ["--oauth-token-type", "JWT"])
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] in ["JWT", "jwt"]

    # Test mixed case Bearer
    captured_env.clear()
    runner.invoke(run, ["--oauth-token-type", "Bearer"])
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] in ["Bearer", "bearer"]


def test_help_includes_stdio_transport(runner):
    """Test that stdio appears as a transport option in help output."""
    result = runner.invoke(run, ["--help"])
    assert result.exit_code == 0
    assert "stdio" in result.output


def test_stdio_rejects_oauth_flag(runner, clean_env, monkeypatch):
    """Test that --transport stdio --oauth raises an error."""
    monkeypatch.setenv("NEXTCLOUD_HOST", "https://cloud.example.com")
    result = runner.invoke(run, ["--transport", "stdio", "--oauth"])
    assert result.exit_code != 0
    assert "stdio transport does not support OAuth mode" in result.output


def test_stdio_calls_get_stdio_mcp(runner, clean_env, monkeypatch):
    """Test that --transport stdio invokes the stdio code path."""
    monkeypatch.setenv("NEXTCLOUD_HOST", "https://cloud.example.com")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "admin")
    monkeypatch.setenv("NEXTCLOUD_PASSWORD", "secret")

    called_with = {}

    class FakeMcp:
        def run(self, transport):
            called_with["transport"] = transport

    def mock_get_stdio_mcp(enabled_apps=None):
        called_with["enabled_apps"] = enabled_apps
        return FakeMcp()

    monkeypatch.setattr("nextcloud_mcp_server.stdio.get_stdio_mcp", mock_get_stdio_mcp)

    result = runner.invoke(run, ["--transport", "stdio"])
    assert result.exit_code == 0, result.output
    assert called_with.get("transport") == "stdio"
    assert called_with.get("enabled_apps") is None


# ---------------------------------------------------------------------------
# Ingest worker observability bootstrap (Deck #310 / #175)
# ---------------------------------------------------------------------------


def _fake_settings(**overrides):
    """A lightweight settings stand-in for the worker observability helper.

    The helper only reads attributes, so a SimpleNamespace avoids running the
    real Settings.__post_init__ validation/derivation.
    """
    base = dict(
        ingest_queue="postgres",  # for realism / worker() gating; unused by the helper
        log_format="json",
        log_level="INFO",
        log_include_trace_context=True,
        metrics_enabled=True,
        metrics_port=9090,
        otel_exporter_otlp_endpoint=None,
        otel_service_name="nextcloud-mcp-server",
        otel_exporter_verify_ssl=False,
        otel_traces_sampler_arg=1.0,
        pyroscope_enabled=False,
        pyroscope_server_address=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def patched_observability(monkeypatch):
    """Patch the worker's observability entrypoints and record their kwargs."""
    calls: dict[str, dict] = {}
    monkeypatch.setattr(
        "nextcloud_mcp_server.cli.setup_logging",
        lambda **kw: calls.__setitem__("logging", kw),
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.cli.setup_metrics",
        lambda **kw: calls.__setitem__("metrics", kw),
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.cli.setup_tracing",
        lambda **kw: calls.__setitem__("tracing", kw),
    )
    monkeypatch.setattr(
        "nextcloud_mcp_server.cli.setup_profiling",
        lambda *a, **kw: calls.__setitem__("profiling", {"args": a, "kwargs": kw}),
    )
    return calls


def test_init_worker_observability_configures_logging(patched_observability):
    """Worker initializes structured logging from settings (AC: JSON logs)."""
    _init_worker_observability(_fake_settings())

    assert patched_observability["logging"] == {
        "log_format": "json",
        "log_level": "INFO",
        "include_trace_context": True,
    }


def test_init_worker_observability_starts_metrics_when_enabled(patched_observability):
    """Worker starts the Prometheus server on the configured port (AC: /metrics)."""
    _init_worker_observability(_fake_settings(metrics_port=9123))

    assert patched_observability["metrics"] == {"port": 9123}


def test_init_worker_observability_skips_metrics_when_disabled(patched_observability):
    """METRICS_ENABLED=false leaves the worker without a metrics server."""
    _init_worker_observability(_fake_settings(metrics_enabled=False))

    assert "metrics" not in patched_observability
    # Logging is still configured regardless of the metrics toggle.
    assert "logging" in patched_observability


def test_init_worker_observability_sets_up_tracing_when_endpoint(
    patched_observability,
):
    """An OTLP endpoint enables tracing so worker spans (parse/embed) export."""
    _init_worker_observability(
        _fake_settings(
            otel_exporter_otlp_endpoint="https://otel:4317",
            otel_traces_sampler_arg=0.5,
        )
    )

    assert patched_observability["tracing"] == {
        "service_name": "nextcloud-mcp-server",
        "otlp_endpoint": "https://otel:4317",
        "otlp_verify_ssl": False,
        "sampling_rate": 0.5,
    }


def test_init_worker_observability_skips_tracing_without_endpoint(
    patched_observability,
):
    """No OTLP endpoint → tracing stays disabled (matches API pod behavior)."""
    _init_worker_observability(_fake_settings(otel_exporter_otlp_endpoint=None))

    assert "tracing" not in patched_observability


def test_init_worker_observability_configures_profiling(patched_observability):
    """Worker wires Pyroscope profiling with the -worker application name and the
    pyroscope_* settings, mirroring the API entrypoint (setup_profiling gates on
    `enabled` internally, so it is always called)."""
    _init_worker_observability(
        _fake_settings(
            pyroscope_enabled=True,
            pyroscope_server_address="alloy.alloy.svc.cluster.local:4041",
        )
    )

    assert patched_observability["profiling"]["kwargs"] == {
        "application_name": "nextcloud-mcp-server-worker",
        "server_address": "alloy.alloy.svc.cluster.local:4041",
        "enabled": True,
    }


def test_worker_initializes_observability_on_postgres_queue(runner, monkeypatch):
    """The worker command wires up observability once config is runnable."""
    monkeypatch.setattr(
        "nextcloud_mcp_server.cli.get_settings",
        lambda: _fake_settings(ingest_queue="postgres"),
    )

    called = {}

    def fake_init(settings):
        called["settings"] = settings
        # Stop before the procrastinate/worker machinery.
        raise SystemExit(0)

    monkeypatch.setattr(
        "nextcloud_mcp_server.cli._init_worker_observability", fake_init
    )

    result = runner.invoke(worker, [])
    assert result.exit_code == 0, result.output
    assert called.get("settings") is not None


def test_worker_rejects_non_postgres_queue_before_observability(runner, monkeypatch):
    """A non-postgres queue fails fast, before any metrics server is started."""
    monkeypatch.setattr(
        "nextcloud_mcp_server.cli.get_settings",
        lambda: _fake_settings(ingest_queue="memory"),
    )

    called = {}
    monkeypatch.setattr(
        "nextcloud_mcp_server.cli._init_worker_observability",
        lambda settings: called.setdefault("init", True),
    )

    result = runner.invoke(worker, [])
    assert result.exit_code != 0
    assert "INGEST_QUEUE=postgres" in result.output
    assert "init" not in called
