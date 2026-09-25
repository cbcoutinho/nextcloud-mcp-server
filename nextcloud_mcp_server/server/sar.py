"""MCP tools for subject access request cases (ADR-040)."""

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.sar import (
    MAX_ITEMS_PER_CALL,
    MAX_QUERIES,
    SarCaseItem,
    SarCaseItemsChange,
    SarCaseListResponse,
    SarCaseResponse,
    SarCaseUpdate,
    SarItemRef,
    SarQueryIn,
    SubjectList,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.redaction import get_ner_client
from nextcloud_mcp_server.sar_case import (
    change_items,
    create_case,
    export_case,
    get_case,
    list_cases,
    update_case,
)
from nextcloud_mcp_server.sar_export import ExportError, background_client

_WRITE = ToolAnnotations(idempotent_hint=False, open_world_hint=True)
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def configure_sar_tools(mcp: MCPServer) -> None:
    @mcp.tool(title="Create SAR Case", annotations=_WRITE)
    @require_scopes("files.write")
    @instrument_tool
    async def sar_case_create(
        ctx: Context,
        folder: str,
        name: str,
        subject: SubjectList,
        description: str = "",
    ) -> SarCaseResponse:
        """Open a subject access request (SAR) case.

        A case collects the documents to disclose about one person, with a
        reason for each, and produces redacted archives of them. It is stored
        as `<folder>/<name>/sar-case.json` in Nextcloud, so anyone who can
        write that folder (e.g. a team folder) can work on it, in Astrolabe or
        through these tools. Use the returned `case_id` with the other
        `sar_case_*` tools.

        Args:
            folder: Existing folder the user can write to, e.g. a team folder.
            name: Case name, e.g. "SAR-2026-014". Becomes a sub-folder.
            subject: The data subject's names, aliases, email addresses, phone
                numbers and NI numbers. These are kept in exports. List every
                alias ("Jane Doe", "Ms Doe", "J. Doe"), as unlisted forms are
                redacted.
            description: Free text, e.g. the request reference.
        """
        client = await get_client(ctx)
        try:
            return await create_case(
                client,
                folder=folder,
                name=name,
                subject=list(subject),
                description=description,
            )
        except ExportError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(title="List SAR Cases", annotations=_READ)
    @require_scopes("files.read")
    @instrument_tool
    async def sar_case_list(ctx: Context) -> SarCaseListResponse:
        """List the SAR cases this user can see, newest first, with their state
        (open, exporting, ready_for_audit, closed) and item count."""
        return await list_cases(await get_client(ctx))

    @mcp.tool(title="Get SAR Case", annotations=_READ)
    @require_scopes("files.read")
    @instrument_tool
    async def sar_case_get(
        ctx: Context,
        case_id: int,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_ITEMS_PER_CALL)] = 200,
    ) -> SarCaseResponse:
        """A SAR case: subject, items (paged by `offset`/`limit`, with
        `items_total`), logged queries, exports, and the latest export's
        progress in `latest_export`. Poll this after `sar_case_export`: the
        case moves to "ready_for_audit" when the archive is written."""
        try:
            return await get_case(await get_client(ctx), case_id, offset, limit)
        except ExportError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(title="Update SAR Case", annotations=_WRITE)
    @require_scopes("files.write")
    @instrument_tool
    async def sar_case_update(
        ctx: Context,
        case_id: int,
        subject: SubjectList | None = None,
        description: str | None = None,
        state: str | None = None,
    ) -> SarCaseResponse:
        """Change a case's subject identifiers or description (open cases
        only), close it, or reopen it.

        Args:
            state: "closed" finishes the case: it becomes read-only and cannot
                be reopened; its archives stay. "open" reopens a case that is
                ready for audit, to change it and export again.
        """
        try:
            update = SarCaseUpdate.model_validate(
                {"subject": subject, "description": description, "state": state}
            )
            return await update_case(await get_client(ctx), case_id, update)
        except (ExportError, ValueError) as e:
            raise ToolError(str(e)) from e

    @mcp.tool(title="Change SAR Case Items", annotations=_WRITE)
    @require_scopes("files.write")
    @instrument_tool
    async def sar_case_items(
        ctx: Context,
        case_id: int,
        add: Annotated[list[SarCaseItem], Field(max_length=MAX_ITEMS_PER_CALL)]
        | None = None,
        remove: Annotated[list[SarItemRef], Field(max_length=MAX_ITEMS_PER_CALL)]
        | None = None,
        queries: Annotated[list[SarQueryIn], Field(max_length=MAX_QUERIES)]
        | None = None,
    ) -> SarCaseResponse:
        """Add, update or remove documents in an open SAR case, and log the
        searches run for it.

        Args:
            add: Documents to include, using `doc_type` and `id` from search
                results as `doc_type`/`doc_id`, each with a `reason` (required
                before export), optionally `title`, `found_by` (the query that
                found it) and a page range for paged files. Adding a document
                already in the case updates it.
            remove: Documents to drop, by `doc_type`/`doc_id`.
            queries: Searches run for the case, including ones that found
                nothing (`text`, optional `hits`); recorded in the archive.
        """
        try:
            request = SarCaseItemsChange(
                add=add or [], remove=remove or [], queries=queries or []
            )
            return await change_items(await get_client(ctx), case_id, request)
        except ExportError as e:
            raise ToolError(str(e)) from e

    @mcp.tool(title="Export SAR Case", annotations=_WRITE)
    @require_scopes("semantic.read", "files.write")
    @instrument_tool
    async def sar_case_export(
        ctx: Context, case_id: int, output_folder: str | None = None
    ) -> SarCaseResponse:
        """Build a redacted archive of an open case, in the background.

        Every person, email address, phone number and UK NI number in the
        documents is replaced with a numbered placeholder ([PERSON_1],
        [EMAIL_1], ...), except the subject's own. The archive holds one PDF
        per document (redacted text, not the original layout), an index with
        each document's reason and redaction counts, and the logged searches.
        The case is locked while exporting; poll `sar_case_get` until it is
        "ready_for_audit". Each export is a new version (`-v1`, `-v2`, ...).

        Args:
            output_folder: Where to write the archive; defaults to the case's
                own `exports/` folder.
        """
        client = await get_client(ctx)
        lifespan_ctx: Any = ctx.request_context.lifespan_context
        ner = await get_ner_client(get_settings())
        try:
            background = await background_client(client.username)
        except ExportError as e:
            raise ToolError(str(e)) from e
        try:
            # export_case owns `background` from here on.
            return await export_case(
                client,
                background,
                ner,
                lifespan_ctx.eviction_task_group,
                case_id,
                output_folder,
            )
        except ExportError as e:
            raise ToolError(str(e)) from e
