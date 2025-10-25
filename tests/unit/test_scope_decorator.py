"""Unit tests for scope decorator metadata and classification logic."""

import pytest

from nextcloud_mcp_server.auth.scope_authorization import (
    InsufficientScopeError,
    require_scopes,
)


@pytest.mark.unit
def test_scope_decorator_stores_metadata():
    """Test that @require_scopes decorator stores scope requirements as function metadata."""

    @require_scopes("notes:read", "notes:write")
    async def example_function():
        pass

    # Verify metadata is stored
    assert hasattr(example_function, "_required_scopes")
    assert example_function._required_scopes == ["notes:read", "notes:write"]


@pytest.mark.unit
def test_scope_decorator_with_single_scope():
    """Test decorator with a single scope requirement."""

    @require_scopes("calendar:read")
    async def example_function():
        pass

    assert example_function._required_scopes == ["calendar:read"]


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
    missing = ["notes:write", "calendar:write"]
    error = InsufficientScopeError(missing)

    assert error.missing_scopes == missing
    assert "notes:write" in str(error)
    assert "calendar:write" in str(error)


@pytest.mark.unit
def test_insufficient_scope_error_with_custom_message():
    """Test InsufficientScopeError with custom message."""
    missing = ["files:write"]
    custom_msg = "You need more permissions"
    error = InsufficientScopeError(missing, custom_msg)

    assert error.missing_scopes == missing
    assert str(error) == custom_msg
