"""Lightweight stdio transport for the Nextcloud MCP server.

Provides a minimal FastMCP instance suitable for ``mcp.run(transport="stdio")``.
Only single-user BasicAuth mode is supported.  Background sync, semantic search,
OAuth, and observability infrastructure are deliberately excluded.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.config_validators import AuthMode, validate_configuration
from nextcloud_mcp_server.context import get_client as get_nextcloud_client
from nextcloud_mcp_server.server import (
    configure_calendar_tools,
    configure_collectives_tools,
    configure_contacts_tools,
    configure_cookbook_tools,
    configure_deck_tools,
    configure_news_tools,
    configure_notes_tools,
    configure_sharing_tools,
    configure_tables_tools,
    configure_webdav_tools,
)

logger = logging.getLogger(__name__)


@dataclass
class StdioContext:
    """Minimal lifespan context for stdio transport.

    Carries only the shared :class:`NextcloudClient`.  The ``client``
    attribute satisfies the duck-type check in
    :func:`nextcloud_mcp_server.context.get_client`.
    """

    client: NextcloudClient


@asynccontextmanager
async def stdio_lifespan(server: FastMCP) -> AsyncIterator[StdioContext]:
    """Create and tear down a single :class:`NextcloudClient`."""
    logger.info("Starting MCP server in stdio mode (single-user BasicAuth)")
    client = NextcloudClient.from_env()
    try:
        yield StdioContext(client=client)
    finally:
        await client.close()
        logger.info("stdio session shut down")


def get_stdio_mcp(enabled_apps: list[str] | None = None) -> FastMCP:
    """Return a :class:`FastMCP` instance configured for stdio transport.

    Parameters
    ----------
    enabled_apps:
        Whitelist of Nextcloud app names to register.  ``None`` means all.

    Raises
    ------
    ValueError
        If the current configuration is not single-user BasicAuth.
    """
    settings = get_settings()
    mode, config_errors = validate_configuration(settings)

    if config_errors:
        raise ValueError(
            f"Configuration validation failed for {mode.value} mode:\n"
            + "\n".join(f"  - {err}" for err in config_errors)
        )

    if mode != AuthMode.SINGLE_USER_BASIC:
        raise ValueError(
            f"stdio transport only supports single-user BasicAuth mode, "
            f"but detected {mode.value}. Set NEXTCLOUD_HOST, NEXTCLOUD_USERNAME, "
            f"and NEXTCLOUD_PASSWORD."
        )

    mcp = FastMCP("Nextcloud MCP", lifespan=stdio_lifespan)

    # --- capabilities resource (mirrors app.py) ---
    @mcp.resource("nc://capabilities")
    async def nc_get_capabilities():
        """Get the Nextcloud Host capabilities"""
        ctx: Context = mcp.get_context()
        client = await get_nextcloud_client(ctx)
        return await client.capabilities()

    # --- tool registration ---
    available_apps: dict[str, Callable] = {
        "notes": configure_notes_tools,
        "tables": configure_tables_tools,
        "webdav": configure_webdav_tools,
        "sharing": configure_sharing_tools,
        "calendar": configure_calendar_tools,
        "collectives": configure_collectives_tools,
        "contacts": configure_contacts_tools,
        "cookbook": configure_cookbook_tools,
        "deck": configure_deck_tools,
        "news": configure_news_tools,
    }

    if enabled_apps is None:
        enabled_apps = list(available_apps.keys())

    for app_name in enabled_apps:
        if app_name in available_apps:
            logger.info(f"Configuring {app_name} tools")
            available_apps[app_name](mcp)
        else:
            logger.warning(
                f"Unknown app: {app_name}. "
                f"Available apps: {list(available_apps.keys())}"
            )

    return mcp
