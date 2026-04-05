"""Unit tests for ClientRegistry ALLOWED_MCP_CLIENTS parsing and validation."""

import logging

import pytest

import nextcloud_mcp_server.auth.client_registry as registry_mod

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the singleton registry before each test."""
    registry_mod._registry = None
    yield
    registry_mod._registry = None


def _get_registry(monkeypatch, value: str | None = None):
    """Helper to create a registry with the given ALLOWED_MCP_CLIENTS value."""
    if value is not None:
        monkeypatch.setenv("ALLOWED_MCP_CLIENTS", value)
    else:
        monkeypatch.delenv("ALLOWED_MCP_CLIENTS", raising=False)
    return registry_mod.get_client_registry()


def test_simple_client_ids(monkeypatch):
    registry = _get_registry(monkeypatch, "claude-desktop, zed-editor")
    clients = registry.list_clients()
    assert len(clients) == 2

    claude = registry.get_client("claude-desktop")
    assert claude is not None
    assert claude.redirect_uris == ["http://localhost:*", "http://127.0.0.1:*"]
    assert claude.allowed_scopes == ["*"]

    zed = registry.get_client("zed-editor")
    assert zed is not None
    assert zed.redirect_uris == ["http://localhost:*", "http://127.0.0.1:*"]


def test_pipe_separated_https(monkeypatch):
    registry = _get_registry(monkeypatch, "myapp|https://app.example.com/callback")
    client = registry.get_client("myapp")
    assert client is not None
    assert client.redirect_uris == ["https://app.example.com/callback"]
    assert client.allowed_scopes == ["*"]


def test_pipe_separated_localhost(monkeypatch):
    registry = _get_registry(monkeypatch, "dev-tool|http://localhost:3000/cb")
    client = registry.get_client("dev-tool")
    assert client is not None
    assert client.redirect_uris == ["http://localhost:3000/cb"]


def test_pipe_separated_loopback_ip(monkeypatch):
    registry = _get_registry(monkeypatch, "dev|http://127.0.0.1:9090/cb")
    client = registry.get_client("dev")
    assert client is not None
    assert client.redirect_uris == ["http://127.0.0.1:9090/cb"]


def test_mixed_entries(monkeypatch):
    registry = _get_registry(
        monkeypatch, "claude-desktop, cloud-app|https://cloud.example.com/cb"
    )
    clients = registry.list_clients()
    assert len(clients) == 2

    claude = registry.get_client("claude-desktop")
    assert claude is not None
    assert claude.redirect_uris == ["http://localhost:*", "http://127.0.0.1:*"]

    cloud = registry.get_client("cloud-app")
    assert cloud is not None
    assert cloud.redirect_uris == ["https://cloud.example.com/cb"]


def test_http_non_localhost_rejected(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        registry = _get_registry(monkeypatch, "bad-client|http://evil.com/cb")

    assert registry.get_client("bad-client") is None
    assert "Rejecting client" in caplog.text
    assert "evil.com" in caplog.text


def test_empty_string_uses_well_known(monkeypatch):
    registry = _get_registry(monkeypatch, "")
    clients = registry.list_clients()
    client_ids = {c.client_id for c in clients}
    assert "claude-desktop" in client_ids
    assert "test-mcp-client" in client_ids


def test_unset_env_uses_well_known(monkeypatch):
    registry = _get_registry(monkeypatch, None)
    clients = registry.list_clients()
    client_ids = {c.client_id for c in clients}
    assert "claude-desktop" in client_ids
    assert "test-mcp-client" in client_ids


def test_malformed_entries_skipped_with_warning(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        registry = _get_registry(monkeypatch, "good, |, , bad|")

    # Only "good" should be registered
    assert registry.get_client("good") is not None
    assert len(registry.list_clients()) == 1
    assert "malformed" in caplog.text.lower()


def test_all_scopes_wildcard(monkeypatch):
    registry = _get_registry(monkeypatch, "test-client")
    client = registry.get_client("test-client")
    assert client is not None
    assert client.allowed_scopes == ["*"]


def test_validate_client_wildcard_scopes(monkeypatch):
    registry = _get_registry(monkeypatch, "test-client")
    valid, err = registry.validate_client(
        "test-client", scopes=["anything", "goes", "here"]
    )
    assert valid is True
    assert err is None


def test_validate_redirect_uri_https_match(monkeypatch):
    registry = _get_registry(monkeypatch, "cloud|https://x.com/cb")
    valid, err = registry.validate_client("cloud", redirect_uri="https://x.com/cb")
    assert valid is True
    assert err is None


def test_validate_redirect_uri_https_mismatch(monkeypatch):
    registry = _get_registry(monkeypatch, "cloud|https://x.com/cb")
    valid, err = registry.validate_client("cloud", redirect_uri="https://other.com/cb")
    assert valid is False
    assert "redirect_uri" in err.lower()


def test_validate_redirect_uri_localhost_wildcard(monkeypatch):
    registry = _get_registry(monkeypatch, "native-client")
    valid, err = registry.validate_client(
        "native-client", redirect_uri="http://localhost:12345/callback"
    )
    assert valid is True
    assert err is None


def test_well_known_clients_wildcard_scopes(monkeypatch):
    registry = _get_registry(monkeypatch, None)
    for client in registry.list_clients():
        assert client.allowed_scopes == ["*"], (
            f"Well-known client {client.client_id} should have wildcard scopes"
        )


def test_client_name_resolution(monkeypatch):
    registry = _get_registry(monkeypatch, "claude-desktop, custom-tool")
    assert registry.get_client("claude-desktop").name == "Claude Desktop"
    assert registry.get_client("custom-tool").name == "Custom Tool"


def test_ipv6_loopback_allowed(monkeypatch):
    registry = _get_registry(monkeypatch, "ipv6-app|http://[::1]:3000/cb")
    client = registry.get_client("ipv6-app")
    assert client is not None
    assert client.redirect_uris == ["http://[::1]:3000/cb"]


def test_malformed_uri_no_hostname_skipped(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        registry = _get_registry(monkeypatch, "bad|http:///no-host")

    assert registry.get_client("bad") is None
    assert "cannot parse hostname" in caplog.text


def test_validate_redirect_uri_ipv6_loopback(monkeypatch):
    """IPv6 loopback redirect URIs should match wildcard localhost patterns."""
    registry = _get_registry(monkeypatch, "ipv6-app|http://[::1]:3000/cb")
    valid, err = registry.validate_client(
        "ipv6-app", redirect_uri="http://[::1]:3000/cb"
    )
    assert valid is True
    assert err is None


def test_validate_redirect_uri_no_hostname(monkeypatch):
    """Redirect URIs with no parseable hostname should be rejected."""
    registry = _get_registry(monkeypatch, "test-client")
    valid, err = registry.validate_client("test-client", redirect_uri="not-a-uri")
    assert valid is False
    assert "redirect_uri" in err.lower()
