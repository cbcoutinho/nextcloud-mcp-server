"""Unit tests for the ``MCP_DISABLED_TOOLS`` operator denylist.

The guarantee under test is that a denylisted tool is gone from the server —
not listed, not callable — while its siblings are untouched, and that a
mistyped name is reported instead of silently disabling nothing.
"""

from __future__ import annotations

import logging

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.config import _DEFAULTS, _reload_config, set_override
from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.server import disabled_tools
from nextcloud_mcp_server.server.disabled_tools import (
    DISABLED_TOOLS_KEY,
    parse_disabled_tools,
    remove_disabled_tools,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def set_disabled_tools():
    """Set MCP_DISABLED_TOOLS, then restore the default so it can't leak."""

    def _apply(value) -> None:
        set_override(DISABLED_TOOLS_KEY, value)
        _reload_config()

    yield _apply
    set_override(DISABLED_TOOLS_KEY, _DEFAULTS[DISABLED_TOOLS_KEY.lower()])
    _reload_config()


def _server() -> NextcloudMCPServer:
    mcp = NextcloudMCPServer("test")

    @mcp.tool()
    async def nc_webdav_read_file() -> str:
        return "read"

    @mcp.tool()
    async def nc_webdav_write_file() -> str:
        return "write"

    @mcp.tool()
    async def nc_webdav_delete_resource() -> str:
        return "delete"

    return mcp


async def _tool_names(mcp: NextcloudMCPServer) -> set[str]:
    return {tool.name for tool in await mcp.list_tools()}


# ---------------------------------------------------------------------------
# parse_disabled_tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", " ", ",", " , ,"])
def test_parse_empty_values_disable_nothing(raw):
    assert parse_disabled_tools(raw) == []


def test_parse_strips_whitespace_and_dedupes_in_order():
    raw = " nc_b , nc_a,,nc_b "
    assert parse_disabled_tools(raw) == ["nc_b", "nc_a"]


def test_parse_accepts_a_list():
    # dynaconf casts a TOML-shaped env value (``["a", "b"]``) to a list.
    assert parse_disabled_tools([" nc_a", "nc_b", ""]) == ["nc_a", "nc_b"]


# ---------------------------------------------------------------------------
# remove_disabled_tools
# ---------------------------------------------------------------------------


async def test_unset_keeps_every_tool():
    mcp = _server()

    assert remove_disabled_tools(mcp) == []
    assert await _tool_names(mcp) == {
        "nc_webdav_read_file",
        "nc_webdav_write_file",
        "nc_webdav_delete_resource",
    }


async def test_disabled_tool_is_neither_listed_nor_callable(set_disabled_tools):
    set_disabled_tools("nc_webdav_delete_resource")
    mcp = _server()

    assert remove_disabled_tools(mcp) == ["nc_webdav_delete_resource"]
    assert await _tool_names(mcp) == {"nc_webdav_read_file", "nc_webdav_write_file"}
    with pytest.raises(ToolError, match="Unknown tool"):
        await mcp.call_tool("nc_webdav_delete_resource", {})


async def test_sibling_tools_still_run(set_disabled_tools):
    set_disabled_tools("nc_webdav_delete_resource")
    mcp = _server()
    remove_disabled_tools(mcp)

    result = await mcp.call_tool("nc_webdav_write_file", {})

    assert not result.is_error


async def test_several_tools_are_disabled(set_disabled_tools):
    set_disabled_tools("nc_webdav_write_file, nc_webdav_delete_resource")
    mcp = _server()

    removed = remove_disabled_tools(mcp)

    assert removed == ["nc_webdav_write_file", "nc_webdav_delete_resource"]
    assert await _tool_names(mcp) == {"nc_webdav_read_file"}


async def test_unknown_name_warns_with_suggestion(set_disabled_tools, caplog):
    set_disabled_tools("nc_webdav_delete_resourse")
    mcp = _server()

    caplog.set_level(logging.WARNING, logger=disabled_tools.__name__)

    removed = remove_disabled_tools(mcp)

    assert removed == []
    assert await _tool_names(mcp) == {
        "nc_webdav_read_file",
        "nc_webdav_write_file",
        "nc_webdav_delete_resource",
    }
    assert "'nc_webdav_delete_resourse'" in caplog.text
    assert "did you mean 'nc_webdav_delete_resource'?" in caplog.text


async def test_unknown_name_does_not_block_the_others(set_disabled_tools):
    set_disabled_tools("nc_notes_create_note, nc_webdav_delete_resource")
    mcp = _server()

    assert remove_disabled_tools(mcp) == ["nc_webdav_delete_resource"]
