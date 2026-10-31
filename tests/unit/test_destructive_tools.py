"""Unit tests for MCP_DISABLE_DESTRUCTIVE_TOOLS.

With the switch on, tools annotated ``destructive_hint=True`` (permanent
deletes) disappear from ``tools/list`` and are refused in ``tools/call``;
everything else, including recoverable operations like moving to trash, is
untouched. With the switch off (the default) nothing changes.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.config import _DEFAULTS, _reload_config, set_override
from nextcloud_mcp_server.errors import NextcloudMCPServer

pytestmark = pytest.mark.unit

_KEY = "MCP_DISABLE_DESTRUCTIVE_TOOLS"


@pytest.fixture
def set_flag():
    """Set the switch to a raw value, then restore the default."""

    def _apply(value) -> None:
        set_override(_KEY, value)
        _reload_config()

    yield _apply
    set_override(_KEY, _DEFAULTS[_KEY.lower()])
    _reload_config()


def _server() -> NextcloudMCPServer:
    """One permanent delete, one recoverable trash, one read."""
    mcp = NextcloudMCPServer("test")

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def nc_notes_delete_note() -> str:
        return "deleted"

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=False))
    async def collectives_trash_page() -> str:
        return "trashed"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def nc_notes_get_note() -> str:
        return "note"

    return mcp


async def test_off_by_default_lists_everything():
    names = {tool.name for tool in await _server().list_tools()}

    assert names == {
        "nc_notes_delete_note",
        "collectives_trash_page",
        "nc_notes_get_note",
    }


async def test_on_hides_only_destructive_tools(set_flag):
    set_flag(True)

    names = {tool.name for tool in await _server().list_tools()}

    assert names == {"collectives_trash_page", "nc_notes_get_note"}


async def test_on_refuses_a_destructive_call_from_a_stale_tool_list(set_flag):
    set_flag(True)

    with pytest.raises(ToolError) as exc_info:
        await _server().call_tool("nc_notes_delete_note", {})

    assert "MCP_DISABLE_DESTRUCTIVE_TOOLS" in str(exc_info.value)


async def test_on_still_runs_recoverable_and_read_tools(set_flag):
    set_flag(True)
    mcp = _server()

    trashed = await mcp.call_tool("collectives_trash_page", {})
    note = await mcp.call_tool("nc_notes_get_note", {})

    assert not trashed.is_error
    assert not note.is_error


@pytest.mark.parametrize("value", ["true", "1", True])
async def test_env_spellings_turn_it_on(set_flag, value):
    set_flag(value)

    names = {tool.name for tool in await _server().list_tools()}

    assert "nc_notes_delete_note" not in names
