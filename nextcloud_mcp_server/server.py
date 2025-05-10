# server.py
import logging
from nextcloud_mcp_server.config import setup_logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from mcp.server.fastmcp import FastMCP, Context
from mcp.server import Server
from collections.abc import AsyncIterator
from nextcloud_mcp_server.client import NextcloudClient
import asyncio  # Import asyncio

setup_logging()

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    client: NextcloudClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage application lifecycle with type-safe context"""
    # Initialize on startup
    logger.info("Creating Nextcloud client")
    client = NextcloudClient.from_env()
    # Add a small delay to allow client initialization to complete
    logger.info("Waiting 2 seconds for client initialization...")
    logger.info("Client initialization wait complete.")
    try:
        yield AppContext(client=client)
    finally:
        # Cleanup on shutdown
        client._client.close()


# Create an MCP server
mcp = FastMCP("Nextcloud MCP", lifespan=app_lifespan)


@mcp.resource("nc://capabilities")
def nc_get_capabilities():
    """Get the Nextcloud Host capabilities"""
    # client = NextcloudClient.from_env()
    ctx = (
        mcp.get_context()
    )  # https://github.com/modelcontextprotocol/python-sdk/issues/244
    client: NextcloudClient = ctx.request_context.lifespan_context.client
    return client.capabilities()


@mcp.resource("notes://settings")
def notes_get_settings():
    """Get the Notes App settings"""
    ctx = (
        mcp.get_context()
    )  # https://github.com/modelcontextprotocol/python-sdk/issues/244
    client: NextcloudClient = ctx.request_context.lifespan_context.client
    return client.notes_get_settings()


@mcp.tool()
def nc_get_note(note_id: int, ctx: Context):
    """Get user note using note id"""
    client: NextcloudClient = ctx.request_context.lifespan_context.client
    return client.notes_get_note(note_id=note_id)


@mcp.tool()
def nc_notes_create_note(title: str, content: str, category: str, ctx: Context):
    """Create a new note"""
    client: NextcloudClient = ctx.request_context.lifespan_context.client
    return client.notes_create_note(
        title=title,
        content=content,
        category=category,
    )


@mcp.tool()
def nc_notes_update_note(
    note_id: int,
    etag: str,
    title: str | None,
    content: str | None,
    category: str | None,
    ctx: Context,
):
    logger.info("Updating note %s", note_id)
    client: NextcloudClient = ctx.request_context.lifespan_context.client
    return client.notes_update_note(
        note_id=note_id,
        etag=etag,
        title=title,
        content=content,
        category=category,
    )


@mcp.tool()
def nc_notes_search_notes(query: str, ctx: Context):
    """Search notes by title or content, returning only id, title, and category."""
    client: NextcloudClient = ctx.request_context.lifespan_context.client
    return client.notes_search_notes(query=query)


@mcp.tool()
def nc_notes_delete_note(note_id: int, ctx: Context):
    logger.info("Deleting note %s", note_id)
    client: NextcloudClient = ctx.request_context.lifespan_context.client
    return client.notes_delete_note(note_id=note_id)


@mcp.resource("nc://Notes/{note_id}/attachments/{attachment_filename}")
def nc_notes_get_attachment(note_id: int, attachment_filename: str):
    """Get a specific attachment from a note"""
    ctx = mcp.get_context()
    client: NextcloudClient = ctx.request_context.lifespan_context.client
    # Assuming a method get_note_attachment exists in the client
    # This method should return the raw content and determine the mime type
    content, mime_type = client.get_note_attachment(
        note_id=note_id, filename=attachment_filename
    )
    return {
        "contents": [
            {
                # Use uppercase 'Notes' to match the decorator
                "uri": f"nc://Notes/{note_id}/attachments/{attachment_filename}",
                "mimeType": mime_type,  # Client needs to determine this
                "data": content,  # Return raw bytes/data
            }
        ]
    }


def run():
    mcp.run()


if __name__ == "__main__":
    logger.info("Starting now")
    mcp.run()
