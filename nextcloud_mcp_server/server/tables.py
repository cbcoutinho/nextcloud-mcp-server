import logging

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


def configure_tables_tools(mcp: FastMCP):
    # Tables tools
    @mcp.tool(
        title="List Tables",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("tables:read")
    @instrument_tool
    async def nc_tables_list_tables(ctx: Context):
        """List all tables available to the user"""
        client = await get_client(ctx)
        return await client.tables.list_tables()

    @mcp.tool(
        title="Get Table Schema",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("tables:read")
    @instrument_tool
    async def nc_tables_get_schema(table_id: int, ctx: Context):
        """Get the schema/structure of a specific table including columns and views"""
        client = await get_client(ctx)
        return await client.tables.get_table_schema(table_id)

    @mcp.tool(
        title="Read Table Rows",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("tables:read")
    @instrument_tool
    async def nc_tables_read_table(
        table_id: int,
        ctx: Context,
        limit: int | None = None,
        offset: int | None = None,
    ):
        """Read rows from a table with optional pagination"""
        client = await get_client(ctx)
        return await client.tables.get_table_rows(table_id, limit, offset)

    @mcp.tool(
        title="Insert Table Row",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("tables:write")
    @instrument_tool
    async def nc_tables_insert_row(table_id: int, data: dict, ctx: Context):
        """Insert a new row into a table.

        Data should be a dictionary mapping column IDs to values, e.g. {1: "text", 2: 42}
        """
        client = await get_client(ctx)
        return await client.tables.create_row(table_id, data)

    @mcp.tool(
        title="Update Table Row",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("tables:write")
    @instrument_tool
    async def nc_tables_update_row(row_id: int, data: dict, ctx: Context):
        """Update an existing row in a table.

        Data should be a dictionary mapping column IDs to new values, e.g. {1: "new text", 2: 99}
        """
        client = await get_client(ctx)
        return await client.tables.update_row(row_id, data)

    @mcp.tool(
        title="Delete Table Row",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("tables:write")
    @instrument_tool
    async def nc_tables_delete_row(row_id: int, ctx: Context):
        """Delete a row from a table"""
        client = await get_client(ctx)
        return await client.tables.delete_row(row_id)
