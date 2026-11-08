import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.tables import (
    GetRowResponse,
    ListTablesResponse,
    ReadTableResponse,
    ReadViewResponse,
    Table,
    TableRow,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


def _row(raw: dict[str, Any], columns: list[int] | None) -> TableRow:
    """Validate a raw row, keeping only ``columns`` (all when None)."""
    row = TableRow.model_validate(raw)
    if columns is not None:
        keep = set(columns)
        row.data = {k: v for k, v in row.data.items() if k in keep}
    return row


def configure_tables_tools(mcp: MCPServer):
    # Tables tools
    @mcp.tool(
        title="List Tables",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("tables.read")
    @instrument_tool
    async def nc_tables_list_tables(ctx: Context) -> ListTablesResponse:
        """List all tables available to the user"""
        client = await get_client(ctx)
        tables_data = await client.tables.list_tables()
        tables = [Table(**t) for t in tables_data]
        return ListTablesResponse(tables=tables, total_count=len(tables))

    @mcp.tool(
        title="Get Table Schema",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("tables.read")
    @instrument_tool
    async def nc_tables_get_schema(table_id: int, ctx: Context):
        """Get the schema/structure of a specific table including columns and views"""
        client = await get_client(ctx)
        return await client.tables.get_table_schema(table_id)

    @mcp.tool(
        title="Read Table Rows",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("tables.read")
    @instrument_tool
    async def nc_tables_read_table(
        table_id: int,
        ctx: Context,
        limit: int | None = None,
        offset: int | None = None,
        columns: list[int] | None = None,
    ) -> ReadTableResponse:
        """Read rows from a table with optional pagination.

        Pass ``columns`` (column IDs from nc_tables_get_schema) to return only
        those cells of each row (omit it for all cells; ``[]`` returns rows
        with no cells, e.g. to list row IDs). To read a filtered/sorted subset, use
        nc_tables_read_view with one of the table's views.
        """
        client = await get_client(ctx)
        rows = await client.tables.get_table_rows(table_id, limit, offset)
        # Wrap in a Response so the rows are one content block, not one per row
        # (GH #568). See nc_tables_list_tables for the same pattern.
        return ReadTableResponse(
            rows=[_row(row, columns) for row in rows],
            table_id=table_id,
            offset=offset,
            limit=limit,
        )

    @mcp.tool(
        title="Read Table View Rows",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("tables.read")
    @instrument_tool
    async def nc_tables_read_view(
        view_id: int,
        ctx: Context,
        limit: int | None = None,
        offset: int | None = None,
        columns: list[int] | None = None,
    ) -> ReadViewResponse:
        """Read the rows of a table view, with the view's filters and sorting applied.

        View IDs are listed under ``views`` in nc_tables_get_schema. Filters and
        sorting are managed by the table owner in the Tables UI. Pass
        ``columns`` (column IDs) to return only those cells of each row (omit
        it for all cells; ``[]`` returns rows with no cells).
        """
        client = await get_client(ctx)
        rows = await client.tables.get_view_rows(view_id, limit, offset)
        return ReadViewResponse(
            rows=[_row(row, columns) for row in rows],
            view_id=view_id,
            offset=offset,
            limit=limit,
        )

    @mcp.tool(
        title="Get Table Row",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("tables.read")
    @instrument_tool
    async def nc_tables_get_row(
        row_id: int, ctx: Context, columns: list[int] | None = None
    ) -> GetRowResponse:
        """Get a single table row by ID.

        Pass ``columns`` (column IDs) to return only those cells (omit it for
        all cells; ``[]`` returns no cells).
        """
        client = await get_client(ctx)
        return GetRowResponse(row=_row(await client.tables.get_row(row_id), columns))

    @mcp.tool(
        title="Insert Table Row",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("tables.write")
    @instrument_tool
    async def nc_tables_insert_row(table_id: int, data: dict, ctx: Context):
        """Insert a new row into a table.

        Data should be a dictionary mapping column IDs to values, e.g. {1: "text", 2: 42}
        """
        client = await get_client(ctx)
        return await client.tables.create_row(table_id, data)

    @mcp.tool(
        title="Update Table Row",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("tables.write")
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
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("tables.write")
    @instrument_tool
    async def nc_tables_delete_row(row_id: int, ctx: Context):
        """Delete a row from a table"""
        client = await get_client(ctx)
        return await client.tables.delete_row(row_id)
