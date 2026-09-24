"""MCP tools for SAR export archives (ADR-040)."""

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.sar import (
    MAX_ITEMS,
    MAX_KEEP,
    MAX_QUERIES,
    Query,
    SarExportRequest,
    SarExportStatus,
    SarItem,
    Subject,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.sar_export import ExportError, read_status, submit_export


def configure_sar_tools(mcp: MCPServer) -> None:
    @mcp.tool(
        title="Submit SAR Export",
        annotations=ToolAnnotations(
            idempotent_hint=False,  # each call writes a new archive
            open_world_hint=True,
        ),
    )
    @require_scopes("semantic.read", "files.write")
    @instrument_tool
    async def sar_export_submit(
        ctx: Context,
        output_folder: str,
        name: str,
        subject: Annotated[list[Subject], Field(min_length=1, max_length=MAX_KEEP)],
        items: Annotated[list[SarItem], Field(min_length=1, max_length=MAX_ITEMS)],
        queries: Annotated[list[Query], Field(max_length=MAX_QUERIES)] | None = None,
    ) -> SarExportStatus:
        """Build a redacted archive of documents for a subject access request.

        Runs in the background. Every person, email address, phone number and
        UK NI number in the documents is replaced with a numbered placeholder
        ([PERSON_1], [EMAIL_1], ...), except the data subject's own, which are
        listed in `subject`. The archive, `<output_folder>/<name>.zip`, holds one
        PDF per document (redacted text, not the original layout), an index with
        each document's reason for inclusion and redaction counts, and the
        search queries if given. Poll `sar_export_status` for progress.

        Args:
            output_folder: Existing folder the user can write to, e.g. a team
                folder. Checked before anything runs.
            name: Archive name, e.g. "SAR-2026-014". Must not already exist.
            subject: The data subject's names, aliases, email addresses, phone
                numbers and NI numbers. These are kept. List every alias
                ("Jane Doe", "Ms Doe", "J. Doe"), as unlisted forms are redacted.
            items: Documents to include, using `doc_type` and `id` from search
                results, each with a reason and optionally a page range.
            queries: The searches that found these documents, for the record.
        """
        client = await get_client(ctx)
        lifespan_ctx: Any = ctx.request_context.lifespan_context
        request = SarExportRequest(
            output_folder=output_folder,
            name=name,
            subject=subject,
            items=items,
            queries=queries or [],
        )
        try:
            return await submit_export(
                client.username, request, lifespan_ctx.eviction_task_group
            )
        except ExportError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(
        title="SAR Export Status",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def sar_export_status(
        ctx: Context, output_folder: str, name: str
    ) -> SarExportStatus:
        """Progress of a SAR export started with `sar_export_submit`.

        `state` is "running", "done" (the archive is at `archive_path`) or
        "failed" (see `message`). A "running" export whose `updated_at` stopped
        moving was interrupted, e.g. by a server restart: submit it again under
        a new name.
        """
        client = await get_client(ctx)
        try:
            return await read_status(client, output_folder, name)
        except ExportError as e:
            raise ToolError(str(e)) from e
