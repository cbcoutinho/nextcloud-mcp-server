"""MCP tools for the Nextcloud Full Text Search app."""

import re
from datetime import UTC, datetime
from typing import Any

from httpx2 import HTTPStatusError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client.fulltextsearch import (
    FullTextSearchError,
    FullTextSearchMatch,
)
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.fulltextsearch import (
    FullTextSearchHit,
    FullTextSearchResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

from .tag_exclusion import get_excluded_file_paths, is_path_excluded

# Nextcloud resolves every hit of a page against the file system before answering.
MAX_LIMIT = 50
MAX_EXCERPTS = 3
MAX_EXCERPT_CHARS = 300

_NO_DOCUMENTS_NOTE = (
    "The index reports hits, but Nextcloud returned no files for them. "
    "files_fulltextsearch 35.0.0 and 35.0.1 drop all hits of users with a dot in "
    "their user id (nextcloud/files_fulltextsearch#389)."
)


def _to_hit(document: dict[str, Any]) -> FullTextSearchHit | None:
    info = document.get("info") or {}
    path = (info.get("path") or "").strip("/")
    if not path:
        return None
    mtime = info.get("mtime")
    excerpts = [
        excerpt["excerpt"].strip()[:MAX_EXCERPT_CHARS]
        for excerpt in document.get("excerpts") or []
        if excerpt.get("excerpt")
    ]
    return FullTextSearchHit(
        path=path,
        mime_type=info.get("mime"),
        size=info.get("size"),
        modified=datetime.fromtimestamp(mtime, UTC).isoformat() if mtime else None,
        excerpts=excerpts[:MAX_EXCERPTS],
    )


def configure_fulltextsearch_tools(mcp: MCPServer) -> None:
    """Register the Full Text Search tools."""

    @mcp.tool(
        title="Full Text Search in Files",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("files.read")
    @instrument_tool
    async def nc_fulltextsearch_search_files(
        query: str,
        ctx: Context,
        extension: str = "",
        match: FullTextSearchMatch = "all",
        limit: int = 20,
        page: int = 1,
    ) -> FullTextSearchResponse:
        """Search the content of files with the full text index of Nextcloud.

        Use this to find documents by what they say, including text the server has
        recognized in scans. The WebDAV search tools only see names and metadata.
        Finds only files the user may access and that are indexed.

        Args:
            query: Words, not case-sensitive. Several words find files with ANY of
                them, ranked by relevance. +word requires a word, -word excludes it,
                "two words" is a phrase, rech* a prefix. Use +word when all words
                must occur.
            extension: Only files with this extension, for example "pdf"
            match: "content" or "filename" limit where to look
            limit: Results per page, at most 50
            page: Page of the results, starting at 1

        Returns:
            The number of all hits in total, and for each hit of the page its path,
            MIME type, size, modification time and short excerpts. Excerpts are not
            the whole document: read the file before relying on its content.
        """
        if not query.strip():
            raise ToolError("query must not be empty")
        extension = extension.strip().lstrip(".")
        if extension and not re.fullmatch(r"[A-Za-z0-9]+", extension):
            raise ToolError("extension may only contain letters and digits")
        if not 1 <= limit <= MAX_LIMIT:
            raise ToolError(f"limit must be between 1 and {MAX_LIMIT}")
        if page < 1:
            raise ToolError("page must be at least 1")

        client = await get_client(ctx)
        try:
            total, documents = await client.fulltextsearch.search_files(
                query, extension=extension, match=match, limit=limit, page=page
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ToolError(
                    "The Nextcloud app fulltextsearch is not available on this server"
                ) from e
            raise ToolError(
                f"Full text search failed: HTTP {e.response.status_code}"
            ) from e
        except FullTextSearchError as e:
            raise ToolError(f"Full text search failed: {e}") from e

        excluded = await get_excluded_file_paths(client.webdav)
        hits = [hit for hit in map(_to_hit, documents) if hit]
        return FullTextSearchResponse(
            query=query,
            total=total,
            page=page,
            limit=limit,
            has_more=page * limit < total,
            results=[h for h in hits if not is_path_excluded(h.path, excluded)],
            note=_NO_DOCUMENTS_NOTE if page == 1 and total and not documents else None,
        )
