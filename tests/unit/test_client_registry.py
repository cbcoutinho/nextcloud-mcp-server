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
    # Config is read via dynaconf (cfg), which loads once — refresh so the env
    # mutation above is reflected before the registry reads it.
    from nextcloud_mcp_server.config import _reload_config

    _reload_config()
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


def test_empty_string_yields_empty_registry(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        registry = _get_registry(monkeypatch, "")
    assert registry.list_clients() == []
    valid, err = registry.validate_client("claude-desktop")
    assert valid is False
    assert "Unknown client_id" in err
    assert "ALLOWED_MCP_CLIENTS is unset or empty" in caplog.text


def test_unset_env_yields_empty_registry(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        registry = _get_registry(monkeypatch, None)
    assert registry.list_clients() == []
    valid, err = registry.validate_client("test-mcp-client")
    assert valid is False
    assert "Unknown client_id" in err


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


def test_client_name_resolution(monkeypatch):
    # Names are derived generically from the client_id — there is no built-in
    # "well-known" client map, so previously special-cased ids now title-case.
    registry = _get_registry(monkeypatch, "claude-desktop, claude-ai, custom-tool")
    assert registry.get_client("claude-desktop").name == "Claude Desktop"
    assert registry.get_client("claude-ai").name == "Claude Ai"
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


# ---------------------------------------------------------------------------
# find_client_for_redirect_uris tests
# ---------------------------------------------------------------------------


def test_find_client_for_redirect_uris_matches_localhost_wildcard(monkeypatch):
    """Static client with localhost:* pattern matches any specific localhost port."""
    registry = _get_registry(monkeypatch, "claude-code-mcp")
    match = registry.find_client_for_redirect_uris(["http://localhost:54321/callback"])
    assert match is not None
    assert match.client_id == "claude-code-mcp"


def test_find_client_for_redirect_uris_matches_loopback_ip(monkeypatch):
    """Static client with 127.0.0.1:* pattern matches any specific loopback port."""
    registry = _get_registry(monkeypatch, "claude-code-mcp")
    match = registry.find_client_for_redirect_uris(["http://127.0.0.1:8765/callback"])
    assert match is not None
    assert match.client_id == "claude-code-mcp"


def test_find_client_for_redirect_uris_no_match(monkeypatch):
    """Returns None when no static client accepts all redirect URIs."""
    registry = _get_registry(monkeypatch, "my-app|https://app.example.com/callback")
    match = registry.find_client_for_redirect_uris(["http://localhost:9999/callback"])
    assert match is None


def test_find_client_for_redirect_uris_ignores_proxy_clients(monkeypatch):
    """DCR-proxy clients (is_static=False) are not returned."""
    registry = _get_registry(monkeypatch, None)
    # Register a proxy client (as the DCR proxy does)
    registry.register_proxy_client(
        client_id="some-uuid-from-idp",
        redirect_uris=["http://localhost:*", "http://127.0.0.1:*"],
        name="Dynamic Client",
    )
    match = registry.find_client_for_redirect_uris(["http://localhost:12345/cb"])
    assert match is None


def test_find_client_for_redirect_uris_ambiguous_returns_none(monkeypatch, caplog):
    """Two static wildcard clients both accept a loopback URI → ambiguous, no guess.

    Loopback redirect URIs are not a client identity (RFC 8252 §7.3/§8.6), so
    when more than one static entry qualifies the short-circuit must refuse to
    guess and fall through, rather than silently returning the first-listed one.
    """
    registry = _get_registry(monkeypatch, "claude-code-mcp, cursor-mcp")
    with caplog.at_level(logging.WARNING):
        match = registry.find_client_for_redirect_uris(["http://localhost:5000/cb"])
    assert match is None
    assert "Ambiguous DCR short-circuit" in caplog.text
    # Both ambiguous candidates are named so the operator can fix their config.
    assert "claude-code-mcp" in caplog.text
    assert "cursor-mcp" in caplog.text


def test_find_client_for_redirect_uris_single_match_with_nonmatching_static(
    monkeypatch,
):
    """The guard counts only *matching* static clients, not the total registered.

    A non-loopback static entry that does not accept the requested URI must not
    make an otherwise-unambiguous loopback match look ambiguous.
    """
    registry = _get_registry(
        monkeypatch, "claude-code-mcp, my-app|https://app.example.com/callback"
    )
    match = registry.find_client_for_redirect_uris(["http://localhost:7777/cb"])
    assert match is not None
    assert match.client_id == "claude-code-mcp"


def test_find_client_for_redirect_uris_empty_list_returns_none(monkeypatch):
    """Empty redirect_uris list returns None (no match without URIs to check)."""
    registry = _get_registry(monkeypatch, "claude-code-mcp")
    match = registry.find_client_for_redirect_uris([])
    assert match is None


def test_find_client_for_redirect_uris_rejects_dict(monkeypatch):
    """A dict value (malformed request body) must not trigger a match."""
    registry = _get_registry(monkeypatch, "claude-code-mcp")
    # A dict is iterable but yields keys, not URIs — should not short-circuit.
    match = registry.find_client_for_redirect_uris({"http://localhost:9999/cb": True})  # type: ignore[arg-type]
    assert match is None


def test_find_client_for_redirect_uris_rejects_list_of_non_strings(monkeypatch):
    """A list containing non-string elements must not trigger a match."""
    registry = _get_registry(monkeypatch, "claude-code-mcp")
    match = registry.find_client_for_redirect_uris([None, 42])  # type: ignore[list-item]
    assert match is None


# ---------------------------------------------------------------------------
# _validate_redirect_uri port validation tests
# ---------------------------------------------------------------------------


def test_validate_redirect_uri_invalid_port_rejected(monkeypatch):
    """Wildcard match must reject URIs with a non-numeric port (e.g. localhost:abc)."""
    registry = _get_registry(monkeypatch, "claude-code-mcp")
    valid, err = registry.validate_client(
        "claude-code-mcp", redirect_uri="http://localhost:abc/callback"
    )
    assert valid is False
    assert "redirect_uri" in err.lower()


def test_validate_redirect_uri_empty_port_rejected(monkeypatch):
    """Wildcard match must reject URIs with an empty port component (e.g. localhost:)."""
    registry = _get_registry(monkeypatch, "claude-code-mcp")
    valid, err = registry.validate_client(
        "claude-code-mcp", redirect_uri="http://localhost:/callback"
    )
    assert valid is False
    assert "redirect_uri" in err.lower()


def test_validate_redirect_uri_valid_port_accepted(monkeypatch):
    """Wildcard match must accept URIs with a valid integer port."""
    registry = _get_registry(monkeypatch, "claude-code-mcp")
    valid, err = registry.validate_client(
        "claude-code-mcp", redirect_uri="http://localhost:8080/callback"
    )
    assert valid is True
    assert err is None
