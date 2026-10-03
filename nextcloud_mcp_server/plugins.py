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
(``load_plugins`` rejects any other), and there is no stable import surface for
the helpers a plugin needs (``get_client``, ``require_scopes``, ...).
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import entry_points

from mcp.server.mcpserver import MCPServer
from starlette.routing import BaseRoute

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nextcloud_mcp_server.plugins"

# A plugin's name becomes the ``<name>_available`` key of /api/v1/status, so it
# must be identifier-like and must not shadow a key the server already reports.
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_RESERVED_NAMES = frozenset({"rerank"})


def _no_routes() -> list[BaseRoute]:
    return []


@dataclass(frozen=True)
class Plugin:
    """An optional feature. ``name`` is lowercase ``[a-z][a-z0-9_]*`` and unique
    across installed plugins."""

    name: str
    available: Callable[[Settings], bool]
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
        try:
            plugin = ep.load()
        except Exception as exc:
            raise RuntimeError(
                f"failed to load plugin entry point {ep.name!r} ({ep.value})"
            ) from exc
        if not isinstance(plugin, Plugin):
            raise TypeError(
                f"entry point {ep.name!r} ({ep.value}) in {ENTRY_POINT_GROUP} "
                f"is a {type(plugin).__name__}, not a Plugin"
            )
        if not _NAME.fullmatch(plugin.name) or plugin.name in _RESERVED_NAMES:
            raise ValueError(
                f"entry point {ep.name!r} ({ep.value}): invalid plugin name "
                f"{plugin.name!r}"
            )
        # ponytail: until scopes are a registry plugins extend (Deck #1381), a
        # plugin may only declare scopes the server already validates; anything
        # else would be advertised via DCR yet rejected everywhere it is used.
        if unknown := plugin.scopes - ALL_SUPPORTED_SCOPES:
            raise ValueError(
                f"plugin {plugin.name!r} declares scopes the server does not "
                f"support: {sorted(unknown)}"
            )
        if plugin.name in plugins:
            raise ValueError(f"two installed plugins are named {plugin.name!r}")
        plugins[plugin.name] = plugin
    return tuple(plugins.values())


def available_plugins(settings: Settings) -> list[Plugin]:
    """The installed plugins that are available under ``settings``."""
    return [p for p in load_plugins() if p.available(settings)]


def register_plugin_tools(mcp: MCPServer, settings: Settings) -> None:
    """Register the MCP tools of every available plugin, logging the skipped
    ones. HTTP transport only: the stdio server supports no plugins yet."""
    for plugin in load_plugins():
        if plugin.available(settings):
            logger.info("Plugin %s: registering tools", plugin.name)
            plugin.register_tools(mcp)
        else:
            logger.info("Plugin %s: not available, skipping", plugin.name)
