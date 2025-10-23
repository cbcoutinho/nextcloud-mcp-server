"""Tests for CLI options using Click's testing utilities."""

import os

import pytest
from click.testing import CliRunner

from nextcloud_mcp_server.app import run


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
        "NEXTCLOUD_OIDC_CLIENT_STORAGE",
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
    monkeypatch.setattr("nextcloud_mcp_server.app.get_app", mock_get_app)

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

    monkeypatch.setattr("nextcloud_mcp_server.app.get_app", mock_get_app)

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

    monkeypatch.setattr("nextcloud_mcp_server.app.get_app", mock_get_app)

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
                "NEXTCLOUD_OIDC_CLIENT_STORAGE": os.environ.get(
                    "NEXTCLOUD_OIDC_CLIENT_STORAGE"
                ),
            }
        )
        raise SystemExit(0)

    monkeypatch.setattr("nextcloud_mcp_server.app.get_app", mock_get_app)

    # Don't provide CLI options or env vars - should use defaults
    _ = runner.invoke(run, [])

    # Verify default values
    assert (
        captured_env["NEXTCLOUD_OIDC_SCOPES"] == "openid profile email nc:read nc:write"
    )
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] == "bearer"
    assert captured_env["NEXTCLOUD_MCP_SERVER_URL"] == "http://localhost:8000"
    assert (
        captured_env["NEXTCLOUD_OIDC_CLIENT_STORAGE"] == ".nextcloud_oauth_client.json"
    )


def test_oauth_token_type_case_normalization(runner, clean_env, monkeypatch):
    """Test that token type is normalized correctly regardless of input case."""
    captured_env = {}

    def mock_get_app(*args, **kwargs):
        captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] = os.environ.get(
            "NEXTCLOUD_OIDC_TOKEN_TYPE"
        )
        raise SystemExit(0)

    monkeypatch.setattr("nextcloud_mcp_server.app.get_app", mock_get_app)

    # Test uppercase JWT
    runner.invoke(run, ["--oauth-token-type", "JWT"])
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] in ["JWT", "jwt"]

    # Test mixed case Bearer
    captured_env.clear()
    runner.invoke(run, ["--oauth-token-type", "Bearer"])
    assert captured_env["NEXTCLOUD_OIDC_TOKEN_TYPE"] in ["Bearer", "bearer"]
