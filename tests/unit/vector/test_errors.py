"""Unit tests for vector-sync error formatting (card 309)."""

import httpx
import pytest

from nextcloud_mcp_server.vector._errors import format_exception_group


@pytest.mark.unit
def test_format_plain_exception_returns_repr():
    exc = httpx.ConnectError("Connection error")
    assert format_exception_group(exc) == repr(exc)


@pytest.mark.unit
def test_format_exception_group_names_leaf_cause():
    """A single-child group must surface the real ConnectError, not the group's
    useless 'unhandled errors in a TaskGroup' default message."""
    leaf = httpx.ConnectError("Connection error")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [leaf])

    formatted = format_exception_group(group)

    assert "ConnectError" in formatted
    # Assert the full leaf repr survives, not just the type name -- guards a
    # future format change that kept the type but dropped the message.
    assert repr(leaf) in formatted
    assert "unhandled errors in a TaskGroup" not in formatted
    assert "1 sub-exception" in formatted


@pytest.mark.unit
def test_format_nested_exception_group_flattens_all_leaves():
    inner = BaseExceptionGroup(
        "inner", [ValueError("bad value"), httpx.ConnectError("conn")]
    )
    outer = BaseExceptionGroup("outer", [inner, RuntimeError("boom")])

    formatted = format_exception_group(outer)

    assert "ValueError" in formatted
    assert "ConnectError" in formatted
    assert "RuntimeError" in formatted
    assert "3 sub-exceptions" in formatted
