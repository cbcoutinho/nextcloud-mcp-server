"""Smithery server factory for stateless deployment.

ADR-016: This module provides a server factory function decorated with
@smithery.server() for Smithery CLI deployment. Session configuration
is automatically handled by Smithery and accessible via ctx.session_config.
"""

import logging
import os

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from smithery.decorators import smithery

from nextcloud_mcp_server.server import (
    configure_calendar_tools,
    configure_contacts_tools,
    configure_cookbook_tools,
    configure_deck_tools,
    configure_notes_tools,
    configure_sharing_tools,
    configure_tables_tools,
    configure_webdav_tools,
)

logger = logging.getLogger(__name__)


class SmitheryConfigSchema(BaseModel):
    """Configuration schema for Smithery session.

    These fields are collected by Smithery's configuration UI and passed
    to the server with each request as session_config.
    """

    nextcloud_url: str = Field(
        ...,
        description="Your Nextcloud instance URL (e.g., https://cloud.example.com)",
    )
    username: str = Field(
        ...,
        description="Your Nextcloud username",
    )
    app_password: str = Field(
        ...,
        description="Nextcloud app password (Settings > Security > App passwords)",
    )


@smithery.server(config_schema=SmitheryConfigSchema)
def create_server():
    """Create and return a FastMCP server instance for Smithery deployment.

    This function is called by Smithery CLI to create the server.
    Session configuration is automatically handled by Smithery and
    accessible via ctx.session_config in tool handlers.
    """
    # Force Smithery mode
    os.environ["SMITHERY_DEPLOYMENT"] = "true"
    os.environ["VECTOR_SYNC_ENABLED"] = "false"

    logger.info("Creating Nextcloud MCP Server for Smithery deployment")

    # Import lifespan after setting env vars
    from nextcloud_mcp_server.app import app_lifespan_smithery

    # Create FastMCP server with Smithery lifespan
    mcp = FastMCP("Nextcloud MCP", lifespan=app_lifespan_smithery)

    # Register all core tools (semantic search is skipped in Smithery mode)
    configure_notes_tools(mcp)
    configure_tables_tools(mcp)
    configure_webdav_tools(mcp)
    configure_sharing_tools(mcp)
    configure_calendar_tools(mcp)
    configure_contacts_tools(mcp)
    configure_cookbook_tools(mcp)
    configure_deck_tools(mcp)

    logger.info("Smithery server configured with core Nextcloud tools")

    return mcp
