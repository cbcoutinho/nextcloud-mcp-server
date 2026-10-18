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

A plugin owns its OAuth scopes (``<prefix>.<action>``, e.g. ``sar.read``):
they join :func:`supported_scopes`, the vocabulary every grant and validation
path checks, whether or not the plugin is available. A plugin also owns its
settings, read with :func:`nextcloud_mcp_server.config.plugin_setting`; when they
are invalid, ``available`` raises, which fails startup.
"""

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import entry_points

from mcp.server.mcpserver import MCPServer
from starlette.routing import BaseRoute

from nextcloud_mcp_server.config import Settings
from nextcloud_mcp_server.models.auth import CORE_SCOPES

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nextcloud_mcp_server.plugins"

# A plugin's name becomes the ``<name>_available`` key of /api/v1/status, so it
# must be identifier-like and must not shadow a key the server already reports.
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_RESERVED_NAMES = frozenset({"rerank"})
_SCOPE = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")


def _no_routes() -> list[BaseRoute]:
    return []


@dataclass(frozen=True)
class Plugin:
    """An optional feature. ``name`` is lowercase ``[a-z][a-z0-9_]*`` and unique
    across installed plugins."""

    name: str
    available: Callable[[Settings], bool]
    """Whether the feature is configured and usable, from settings alone. Raises
    ``ValueError`` when the plugin is enabled but misconfigured: it is first
    called at startup, so that fails the server rather than hiding the feature."""
    register_tools: Callable[[MCPServer], None]
    routes: Callable[[], list[BaseRoute]] = _no_routes
    """HTTP routes, mounted alongside the authenticated management API (so only
    in OAuth and multi-user BasicAuth-with-offline-access modes). Handlers
    authenticate requests themselves."""
    scopes: frozenset[str] = field(default_factory=frozenset)
    """The plugin's own OAuth scopes, ``<prefix>.<action>``. Always grantable,
    advertised via DCR only while the plugin is available."""


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
        if plugin.name in plugins:
            raise ValueError(f"two installed plugins are named {plugin.name!r}")
        # A plugin's scopes are its own: withholding one from DCR while the
        # plugin is unavailable must not withhold a core or another plugin's.
        taken = CORE_SCOPES.union(*(p.scopes for p in plugins.values()))
        if bad := sorted(
            s for s in plugin.scopes if s in taken or not _SCOPE.fullmatch(s)
        ):
            raise ValueError(
                f"plugin {plugin.name!r}: scopes {bad} are malformed or already "
                "taken by the server or another plugin"
            )
        plugins[plugin.name] = plugin
    return tuple(plugins.values())


def supported_scopes() -> frozenset[str]:
    """Every grantable scope: the server's own plus each installed plugin's."""
    return CORE_SCOPES.union(*(p.scopes for p in load_plugins()))


def available_plugins(settings: Settings) -> list[Plugin]:
    """The installed plugins that are available under ``settings``."""
    return [p for p in load_plugins() if p.available(settings)]


@contextmanager
def _blame(plugin: Plugin, step: str) -> Iterator[None]:
    """Re-raise a plugin's own failure naming the plugin, as load_plugins does."""
    try:
        yield
    except Exception as exc:
        raise RuntimeError(f"plugin {plugin.name!r} failed to {step}") from exc


def check_plugins(settings: Settings) -> None:
    """Load every plugin and check its settings, logging each. Run at startup,
    so a broken or misconfigured plugin fails it."""
    for plugin in load_plugins():
        with _blame(plugin, "check its settings"):
            available = plugin.available(settings)
        logger.info("Plugin installed: %s (available: %s)", plugin.name, available)


def register_plugin_tools(mcp: MCPServer, settings: Settings) -> None:
    """Register the MCP tools of every available plugin, logging the skipped
    ones. HTTP transport only: the stdio server supports no plugins yet."""
    for plugin in load_plugins():
        if plugin.available(settings):
            logger.info("Plugin %s: registering tools", plugin.name)
            with _blame(plugin, "register its tools"):
                plugin.register_tools(mcp)
        else:
            logger.info("Plugin %s: not available, skipping", plugin.name)


def plugin_routes(settings: Settings) -> list[BaseRoute]:
    """The HTTP routes of every available plugin."""
    routes: list[BaseRoute] = []
    for plugin in available_plugins(settings):
        with _blame(plugin, "build its routes"):
            routes += plugin.routes()
        logger.info("Plugin %s: HTTP routes enabled", plugin.name)
    return routes
