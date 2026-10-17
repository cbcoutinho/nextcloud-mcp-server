"""Operator denylist for individual MCP tools (``MCP_DISABLED_TOOLS``).

``--enable-app`` works per app and OAuth scopes work per read/write family, so
neither can drop one tool while keeping its siblings — e.g. keep
``nc_webdav_write_file`` but never expose ``nc_webdav_delete_resource``, whose
shared ``files.write`` scope a user can re-grant via ``nc_auth_update_scopes``.

The listed tools are *unregistered* once every tool has been registered, so
they are absent from ``tools/list`` and ``tools/call`` answers "Unknown tool"
in every deployment mode, for every user and every token scope. There is no
per-request cost, and no client-side setting can bring them back.
"""

import difflib
import logging

from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.config import cfg

logger = logging.getLogger(__name__)

DISABLED_TOOLS_KEY = "MCP_DISABLED_TOOLS"


def parse_disabled_tools(raw: object) -> list[str]:
    """Split a comma-separated ``MCP_DISABLED_TOOLS`` value into tool names.

    Whitespace and empty entries are dropped and duplicates collapsed, keeping
    first-seen order so the startup log reads like the operator's config.
    dynaconf may already hand back a list when the value is TOML-shaped
    (``["a", "b"]``), so both forms are accepted.
    """
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else str(raw).split(",")
    names = (str(item).strip() for item in items)
    return list(dict.fromkeys(name for name in names if name))


def remove_disabled_tools(mcp: MCPServer) -> list[str]:
    """Unregister every tool named in ``MCP_DISABLED_TOOLS``; return those removed.

    Call it after the last tool is registered. A name that matches no
    registered tool is logged as a warning with the closest match rather than
    failing startup: the tool may belong to an app not enabled on this
    deployment, and a typo must be loud without taking the server down.
    """
    names = parse_disabled_tools(cfg(DISABLED_TOOLS_KEY))
    if not names:
        return []

    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    removed: list[str] = []
    for name in names:
        if name in registered:
            mcp.remove_tool(name)
            removed.append(name)
            continue
        suggestion = difflib.get_close_matches(name, registered, n=1)
        if suggestion:
            logger.warning(
                "%s: no registered tool named %r (did you mean %r?)",
                DISABLED_TOOLS_KEY,
                name,
                suggestion[0],
            )
        else:
            logger.warning("%s: no registered tool named %r", DISABLED_TOOLS_KEY, name)

    if removed:
        logger.info(
            "%s: disabled %d tool(s): %s",
            DISABLED_TOOLS_KEY,
            len(removed),
            ", ".join(removed),
        )
    return removed
