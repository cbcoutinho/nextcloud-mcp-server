"""MCP tools backed by the Nextcloud FullTextSearch app."""

import logging

from httpx import HTTPStatusError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models import FullTextSearchHit, FullTextSearchResponse
from nextcloud_mcp_server.observability.metrics import instrument_tool

from .tag_exclusion import get_excluded_file_paths, is_path_excluded

logger = logging.getLogger(__name__)

_EMPTY_HINT = (
    "FullTextSearch returned no results. Confirm the 'fulltextsearch' app is "
    "enabled, a search platform (e.g. Electra/Elasticsearch) is configured, "
    "content has been indexed, and a file-indexing provider such as "
    "'files_fulltextsearch' is installed. Try narrowing 'providers'."
)


def configure_fulltextsearch_tools(mcp: MCPServer):
    """Register the FullTextSearch-driven file-discovery tools."""

    @mcp.tool(
        title="Find Files by Content (Full-Text Search)",
        annotations=ToolAnnotations(
            read_only_hint=True,
            open_world_hint=True,
        ),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_fulltextsearch_find_files(
        ctx: Context,
        term: str,
        providers: list[str] | None = None,
        limit: int = 10,
        page: int = 1,
    ) -> FullTextSearchResponse:
        """Find files whose indexed content matches a free-text term.

        Uses the Nextcloud FullTextSearch app, which searches the *contents* of
        indexed documents — unlike name/metadata search. Each hit that exposes a
        ``path`` can be handed straight to the WebDAV read/download tools; others
        carry only a browser ``url``.

        Requires the 'fulltextsearch' app with a configured search platform and a
        file-indexing provider; if none is set up the result set will be empty
        and `note` explains why.

        Args:
            term: Free-text query matched against indexed document content.
            providers: Optional provider ids to restrict the search to (e.g.
                ["files"]). Empty/omitted searches every provider the instance
                has configured.
            limit: Maximum results to return (maps to the app's page size).
            page: 1-based page for pagination through a large result set.

        Returns:
            FullTextSearchResponse with flattened, provider-aware hits.
        """
        if not term or not term.strip():
            raise ToolError("term must not be empty or whitespace-only")

        size = max(limit, 1)
        client = await get_client(ctx)

        try:
            hits = await client.fulltextsearch.search(
                term=term,
                providers=providers,
                page=max(page, 1),
                size=size,
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ToolError(
                    "The Nextcloud 'fulltextsearch' app is not available at this "
                    "instance (its search endpoint returned 404). Install and "
                    "enable it to use full-text file search."
                ) from e
            raise

        # Honour tag-based exclusions for any hit that resolves to a file path,
        # so full-text search cannot surface content the WebDAV tools hide.
        excluded = await get_excluded_file_paths(client.webdav)
        if excluded:
            hits = [
                h
                for h in hits
                if not (h.get("path") and is_path_excluded(h["path"], excluded))
            ]

        result_models = [FullTextSearchHit(**hit) for hit in hits]

        return FullTextSearchResponse(
            query=term,
            results=result_models,
            total_found=len(result_models),
            providers=list(providers or []),
            note=_EMPTY_HINT if not result_models else None,
        )
