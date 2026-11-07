"""Operator switch that keeps destructive tools away from MCP clients.

With ``MCP_DISABLE_DESTRUCTIVE_TOOLS=true`` every tool annotated
``destructive_hint=True`` (permanent deletes, per ADR-017) is hidden from
``tools/list`` and refused in ``tools/call``. Recoverable operations -- moving
to trash, restoring, removing a tag -- are not marked destructive and stay
available, so an agent can still tidy up while nothing it does is permanent.

Useful for deployments that hand an agent broad write access but want deletes
to stay a human action. Off by default.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.config import cfg_bool


def destructive_tools_disabled() -> bool:
    return cfg_bool("MCP_DISABLE_DESTRUCTIVE_TOOLS")


def _is_destructive(tool: Any) -> bool:
    annotations = getattr(tool, "annotations", None)
    return bool(annotations is not None and annotations.destructive_hint)


def hide_destructive_tools(tools: list) -> list:
    """Drop destructive tools from a listing when the switch is on."""
    if not destructive_tools_disabled():
        return tools
    return [tool for tool in tools if not _is_destructive(tool)]


def refuse_destructive_tool(mcp: Any, name: str) -> None:
    """Refuse a destructive tool when the switch is on.

    ``hide_destructive_tools`` already keeps it out of the listing, but a client
    may hold a stale tool list -- an explicit refusal beats running it.
    """
    if not destructive_tools_disabled():
        return
    tool = mcp._tool_manager.get_tool(name)
    if tool is not None and _is_destructive(tool):
        raise ToolError(
            f"Tool {name} is disabled on this server: it permanently deletes data, "
            "and MCP_DISABLE_DESTRUCTIVE_TOOLS is set. Ask the user to do it in "
            "Nextcloud, or use a recoverable alternative such as moving to trash."
        )
