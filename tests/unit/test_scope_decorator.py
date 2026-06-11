"""Unit tests for scope decorator metadata and classification logic."""

import pytest
from mcp.server.fastmcp import FastMCP

from nextcloud_mcp_server.auth.scope_authorization import (
    InsufficientScopeError,
    discover_all_scopes,
    require_scopes,
)


@pytest.mark.unit
def test_scope_decorator_stores_metadata():
    """Test that @require_scopes decorator stores scope requirements as function metadata."""

    @require_scopes("notes.read", "notes.write")
    async def example_function():
        pass

    # Verify metadata is stored
    assert hasattr(example_function, "_required_scopes")
    assert example_function._required_scopes == ["notes.read", "notes.write"]


@pytest.mark.unit
def test_scope_decorator_with_single_scope():
    """Test decorator with a single scope requirement."""

    @require_scopes("calendar.read")
    async def example_function():
        pass

    assert example_function._required_scopes == ["calendar.read"]


@pytest.mark.unit
def test_scope_decorator_with_no_scopes():
    """Test decorator with no scope requirements."""

    @require_scopes()
    async def example_function():
        pass

    assert example_function._required_scopes == []


@pytest.mark.unit
def test_insufficient_scope_error():
    """Test InsufficientScopeError exception structure."""
    missing = ["notes.write", "calendar.write"]
    error = InsufficientScopeError(missing)

    assert error.missing_scopes == missing
    assert "notes.write" in str(error)
    assert "calendar.write" in str(error)


@pytest.mark.unit
def test_insufficient_scope_error_with_custom_message():
    """Test InsufficientScopeError with custom message."""
    missing = ["files.write"]
    custom_msg = "You need more permissions"
    error = InsufficientScopeError(missing, custom_msg)

    assert error.missing_scopes == missing
    assert str(error) == custom_msg


@pytest.mark.unit
def test_discover_all_scopes_always_includes_offline_access():
    """offline_access is advertised so discovery-driven clients can request a refresh token.

    It is not tied to any tool's @require_scopes, so it must be present even on
    an MCP instance with a single unrelated tool. Guards the metadata exposed at
    /.well-known/oauth-protected-resource and /.well-known/oauth-authorization-server.
    """
    mcp = FastMCP(name="test-scope-discovery")

    @mcp.tool()
    @require_scopes("notes.read")
    async def example_tool():
        pass

    scopes = discover_all_scopes(mcp)

    assert "offline_access" in scopes
    # Base OIDC scopes and tool-derived scopes still come through.
    assert {"openid", "profile", "email", "notes.read"}.issubset(scopes)


@pytest.mark.unit
def test_discover_all_scopes_offline_access_without_any_tools():
    """The offline_access invariant must not depend on any tool being registered."""
    mcp = FastMCP(name="empty")

    scopes = discover_all_scopes(mcp)

    assert "offline_access" in scopes
    assert {"openid", "profile", "email"}.issubset(scopes)
