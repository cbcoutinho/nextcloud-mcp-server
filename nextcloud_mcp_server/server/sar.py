"""MCP tools for SAR export archives (ADR-040)."""

from typing import Annotated, Any

from httpx import HTTPStatusError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.sar import (
    MAX_ITEMS,
    MAX_KEEP,
    MAX_QUERIES,
    SarExportStatus,
    SarItem,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.redaction import get_ner_client
from nextcloud_mcp_server.sar_export import (
    ExportError,
    archive_paths,
    read_status,
    start_export,
)
from nextcloud_mcp_server.vector.oauth_sync import (
    NotProvisionedError,
    resolve_background_client,
)

Subject = Annotated[str, Field(min_length=1, max_length=200)]
Query = Annotated[str, Field(min_length=1, max_length=1000)]


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
        settings = get_settings()
        client = await get_client(ctx)
        lifespan_ctx: Any = ctx.request_context.lifespan_context
        task_group = lifespan_ctx.eviction_task_group
        if task_group is None:
            raise ToolError("SAR export is unavailable: background tasks not running")
        try:
            archive_paths(output_folder, name)  # validate before any I/O
            background = await resolve_background_client(client.username)
        except ExportError as e:
            raise ToolError(str(e)) from e
        except NotProvisionedError as e:
            raise ToolError(
                "SAR export runs in the background and needs background access: "
                "provision it in Astrolabe's personal settings."
            ) from e
        try:
            # On success the job owns `background` and closes it.
            return await start_export(
                nc=background,
                ner=await get_ner_client(settings),
                task_group=task_group,
                output_folder=output_folder,
                name=name,
                keep=list(subject),
                items=items,
                queries=list(queries or []),
            )
        except ExportError as e:
            await background.close()
            raise ToolError(str(e)) from e
        except HTTPStatusError as e:
            await background.close()
            raise ToolError(
                f"Cannot write to {output_folder!r} "
                f"(HTTP {e.response.status_code}): it must exist and be writable."
            ) from e
        except BaseException:
            await background.close()
            raise

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
            _, status_path = archive_paths(output_folder, name)
            return await read_status(client, status_path)
        except ExportError as e:
            raise ToolError(str(e)) from e
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ToolError(f"No export named {name!r} in {output_folder!r}") from e
            raise
