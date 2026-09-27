"""MCP tools for subject access request cases (ADR-040)."""

import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypeVar, cast

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
    SarSearchFilters,
    SubjectList,
)
from nextcloud_mcp_server.models.semantic import SemanticSearchResponse
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
from nextcloud_mcp_server.search.access_filter import MAX_PATH_PREFIXES

_WRITE = ToolAnnotations(idempotent_hint=False, open_world_hint=True)
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)

_Tool = TypeVar("_Tool", bound=Callable[..., Awaitable[Any]])


def _tool_errors(fn: _Tool) -> _Tool:
    """Report a refused operation (wrong state, bad input, no access) as a tool
    error carrying its message, rather than as an internal error."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (ExportError, ValueError) as e:
            raise ToolError(str(e)) from e

    return cast(_Tool, wrapper)


def configure_sar_tools(mcp: MCPServer) -> None:
    @mcp.tool(title="Create SAR Case", annotations=_WRITE)
    @require_scopes("sar.write", "files.write")
    @instrument_tool
    @_tool_errors
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
                numbers, NI numbers and addresses. These are kept in exports. List every
                alias ("Jane Doe", "Ms Doe", "J. Doe"), as unlisted forms are
                redacted.
            description: Free text, e.g. the request reference.
        """
        return await create_case(
            await get_client(ctx),
            folder=folder,
            name=name,
            subject=list(subject),
            description=description,
        )

    @mcp.tool(title="List SAR Cases", annotations=_READ)
    @require_scopes("sar.read", "files.read")
    @instrument_tool
    async def sar_case_list(ctx: Context) -> SarCaseListResponse:
        """List the SAR cases this user can see, newest first, with their state
        (open, exporting, ready_for_audit, closed) and item count."""
        return await list_cases(await get_client(ctx))

    @mcp.tool(title="Get SAR Case", annotations=_READ)
    @require_scopes("sar.read", "files.read")
    @instrument_tool
    @_tool_errors
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
        return await get_case(await get_client(ctx), case_id, offset, limit)

    @mcp.tool(title="Update SAR Case", annotations=_WRITE)
    @require_scopes("sar.write", "files.write")
    @instrument_tool
    @_tool_errors
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
                be reopened. Its archives stay. "open" reopens a case that is
                ready for audit, to change it and export again.
        """
        update = SarCaseUpdate.model_validate(
            {"subject": subject, "description": description, "state": state}
        )
        return await update_case(await get_client(ctx), case_id, update)

    @mcp.tool(title="Change SAR Case Items", annotations=_WRITE)
    @require_scopes("sar.write", "files.write")
    @instrument_tool
    @_tool_errors
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
                nothing (`text`, optional `hits`). They are recorded in the archive.
        """
        request = SarCaseItemsChange(
            add=add or [], remove=remove or [], queries=queries or []
        )
        return await change_items(await get_client(ctx), case_id, request)

    @mcp.tool(title="Search for a SAR Case", annotations=_WRITE)
    @require_scopes("sar.write", "semantic.read", "files.write")
    @instrument_tool
    @_tool_errors
    # S107 (too many parameters): the parameter list is the tool's wire schema,
    # the same filters nc_semantic_search takes.
    async def sar_case_search(  # NOSONAR(S107)
        ctx: Context,
        case_id: int,
        query: str,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        doc_types: list[str] | None = None,
        path_prefixes: Annotated[
            list[str] | None, Field(max_length=MAX_PATH_PREFIXES)
        ] = None,
        modified_after: str | int | None = None,
        modified_before: str | int | None = None,
        min_relevance: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0,
        score_threshold: Annotated[float, Field(ge=0.0)] = 0.0,
        fusion: str = "rrf",
        rerank: bool = False,
        granularity: Literal["chunk", "document"] = "document",
    ) -> SemanticSearchResponse:
        """Search for documents for an open SAR case, and log the search in it.

        Takes the same filters as `nc_semantic_search` (folders, document
        types, modified dates, relevance) and returns the same results. The
        query and its filters are recorded in the case and listed, redacted,
        in the archive. Results default to one row per document. Add the ones
        to disclose with `sar_case_items`.
        """
        # Lazy: the semantic tools module pulls in the search stack.
        from nextcloud_mcp_server.server.semantic import (  # noqa: PLC0415
            nc_semantic_search,
        )

        filters = SarSearchFilters(
            doc_types=doc_types,
            path_prefixes=path_prefixes,
            modified_after=modified_after,
            modified_before=modified_before,
            min_relevance=min_relevance or None,
            score_threshold=score_threshold or None,
            fusion=fusion,
            rerank=rerank,
            granularity=granularity,
        )
        result = await nc_semantic_search(
            query=query,
            ctx=ctx,
            limit=limit,
            doc_types=doc_types,
            score_threshold=score_threshold,
            min_relevance=min_relevance,
            fusion=fusion,
            granularity=granularity,
            rerank=rerank,
            modified_after=modified_after,
            modified_before=modified_before,
            path_prefixes=path_prefixes,
        )
        log = SarQueryIn(text=query, hits=result.total_found, filters=filters)
        # No record, no results: a search for a case must be logged in it, so
        # a refused log (closed case, no access) fails the call.
        await change_items(
            await get_client(ctx), case_id, SarCaseItemsChange(queries=[log])
        )
        return result

    @mcp.tool(title="Export SAR Case", annotations=_WRITE)
    @require_scopes("sar.write", "semantic.read", "files.write")
    @instrument_tool
    @_tool_errors
    async def sar_case_export(
        ctx: Context, case_id: int, output_folder: str | None = None
    ) -> SarCaseResponse:
        """Build a redacted archive of an open case, in the background.

        Every person, address, UK postcode, email address, phone number and UK NI
        number in the documents is replaced with a numbered placeholder ([PERSON_1],
        [ADDRESS_1], ...), except the subject's own. The archive holds one PDF
        per document (redacted text, not the original layout), an index with
        each document's reason and redaction counts, and the logged searches.
        The case is locked while exporting. Poll `sar_case_get` until it is
        "ready_for_audit". Each export is a new version (`-v1`, `-v2`, ...).

        Args:
            output_folder: Where to write the archive. Defaults to the case's
                own `exports/` folder.
        """
        client = await get_client(ctx)
        lifespan_ctx: Any = ctx.request_context.lifespan_context
        ner = await get_ner_client(get_settings())
        background = await background_client(client.username)
        # export_case owns `background` from here on.
        return await export_case(
            client,
            background,
            ner,
            lifespan_ctx.eviction_task_group,
            case_id,
            output_folder,
        )
