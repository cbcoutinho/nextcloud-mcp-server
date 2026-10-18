"""The server's public surface for plugins (see :mod:`nextcloud_mcp_server.plugins`).

A plugin imports from ``nextcloud_mcp_server`` only through this module; every
other module is internal and may change in any release. A name here changes only
with a ``BREAKING CHANGE`` entry in the CHANGELOG.

Importing this module is cheap: the names in ``_LAZY`` import the module that
defines them (some need the ``semantic`` extra, or the app) on first use, so a
plugin's entry-point module can import ``plugin_api`` at module level. Import
the heavy names inside ``register_tools`` / ``routes`` code, as
``nextcloud_mcp_sar.plugin`` does.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from nextcloud_mcp_server.config import Settings, get_settings, plugin_setting
from nextcloud_mcp_server.features import gateway_v1_url
from nextcloud_mcp_server.models.base import BaseResponse
from nextcloud_mcp_server.plugins import Plugin

if TYPE_CHECKING:
    pass

# name -> (module, attribute)
_LAZY: dict[str, tuple[str, str]] = {
    # MCP tools
    "NextcloudClient": ("client", "NextcloudClient"),
    "get_client": ("context", "get_client"),
    "require_scopes": ("auth", "require_scopes"),
    "instrument_tool": ("observability.metrics", "instrument_tool"),
    "like_predicate": ("client.webdav", "like_predicate"),
    "SemanticSearchResponse": ("models.semantic", "SemanticSearchResponse"),
    # HTTP routes
    "authenticate": ("api.management", "authenticate"),
    "sanitize_error_for_client": ("api.management", "_sanitize_error_for_client"),
    # Background work, outliving the request
    "background_task_group": ("app", "background_task_group"),
    "resolve_background_client": ("vector.oauth_sync", "resolve_background_client"),
    "NotProvisionedError": ("vector.oauth_sync", "NotProvisionedError"),
    # Embedding gateway
    "GatewayTokenProvider": ("providers.gateway", "GatewayTokenProvider"),
    "build_gateway_token_provider": (
        "providers.gateway",
        "build_gateway_token_provider",
    ),
    # Semantic search and the index (need the semantic extra)
    "semantic_search": ("server.semantic", "nc_semantic_search"),
    "unified_search": ("api.visualization", "unified_search"),
    "indexed_chunks": ("search.context", "indexed_chunks"),
    "list_accessible_owners": ("search.access_filter", "list_accessible_owners"),
    "normalize_path_prefixes": ("search.access_filter", "normalize_path_prefixes"),
    "MAX_PATH_PREFIXES": ("search.access_filter", "MAX_PATH_PREFIXES"),
}

__all__ = [
    "BaseResponse",
    "Plugin",
    "Settings",
    "gateway_v1_url",
    "get_settings",
    "plugin_setting",
    *_LAZY,
]


def __getattr__(name: str) -> Any:
    try:
        module, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(import_module(f"nextcloud_mcp_server.{module}"), attr)
