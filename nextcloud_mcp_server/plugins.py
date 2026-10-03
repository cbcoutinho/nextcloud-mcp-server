"""Plugins: optional features that register into the server via entry points.

A plugin is a :class:`Plugin` instance published under the
``nextcloud_mcp_server.plugins`` entry-point group by any installed
distribution, this one included::

    [project.entry-points."nextcloud_mcp_server.plugins"]
    sar = "nextcloud_mcp_server.sar_plugin:plugin"

The server loads every plugin at startup and, for each one whose
``available(settings)`` is true, registers its MCP tools, mounts its HTTP
routes and advertises its OAuth scopes. ``/api/v1/status`` reports
``<name>_available`` per installed plugin.

**The module an entry point names must be cheap to import.** It is loaded on
every start, including deployments where the plugin is unavailable or its
optional dependencies are not installed. Keep heavy imports inside
``register_tools`` / ``routes``, which only run when ``available`` is true —
see ``sar_plugin.py``.

Known gaps before a plugin can live outside this repository (Deck #1381):
the scopes a plugin declares must also be in ``models.auth.ALL_SUPPORTED_SCOPES``
for scope validation to accept them, and there is no stable import surface for
the helpers a plugin needs (``get_client``, ``require_scopes``, ...).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import entry_points
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.routing import BaseRoute

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nextcloud_mcp_server.plugins"


def _no_routes() -> list[BaseRoute]:
    return []


@dataclass(frozen=True)
class Plugin:
    """An optional feature. ``name`` must be unique across installed plugins."""

    name: str
    available: Callable[[Any], bool]
    """Whether the feature is configured and usable, from settings alone."""
    register_tools: Callable[[MCPServer], None]
    routes: Callable[[], list[BaseRoute]] = _no_routes
    """HTTP routes, mounted alongside the authenticated management API (so only
    in OAuth and multi-user BasicAuth-with-offline-access modes). Handlers
    authenticate requests themselves."""
    scopes: frozenset[str] = field(default_factory=frozenset)
    """OAuth scopes advertised via DCR only while the plugin is available."""


@cache
def load_plugins() -> tuple[Plugin, ...]:
    """Every installed plugin, loaded once per process."""
    plugins: dict[str, Plugin] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        plugin = ep.load()
        if not isinstance(plugin, Plugin):
            raise TypeError(
                f"entry point {ep.name!r} ({ep.value}) in {ENTRY_POINT_GROUP} "
                f"is a {type(plugin).__name__}, not a Plugin"
            )
        if plugin.name in plugins:
            raise ValueError(f"two installed plugins are named {plugin.name!r}")
        plugins[plugin.name] = plugin
    return tuple(plugins.values())


def available_plugins(settings: Any) -> list[Plugin]:
    """The installed plugins that are available under ``settings``."""
    return [p for p in load_plugins() if p.available(settings)]


def register_plugin_tools(mcp: MCPServer, settings: Any) -> None:
    """Register the MCP tools of every available plugin. Shared by both
    transports (app.py, stdio.py) so their tool sets cannot drift."""
    for plugin in load_plugins():
        if plugin.available(settings):
            logger.info("Plugin %s: registering tools", plugin.name)
            plugin.register_tools(mcp)
        else:
            logger.info("Plugin %s: not available, skipping", plugin.name)
