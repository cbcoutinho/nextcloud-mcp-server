"""MCP tool definitions for Nextcloud Collectives app."""

import logging

from httpx import HTTPStatusError
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client.collectives import OCSError
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.collectives import (
    Collective,
    CollectiveOperationResponse,
    CollectiveTag,
    CreateCollectiveResponse,
    CreatePageResponse,
    CreateTagResponse,
    GetPageResponse,
    ListCollectivesResponse,
    ListPagesResponse,
    ListTagsResponse,
    ListTrashedPagesResponse,
    PageInfo,
    PageOperationResponse,
    SearchPagesResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


def _handle_collectives_error(e: OCSError | HTTPStatusError) -> McpError:
    """Convert OCS or HTTP errors to McpError."""
    if isinstance(e, OCSError):
        return McpError(ErrorData(code=-32603, message=e.message))
    return McpError(ErrorData(code=-32603, message=str(e)))


def configure_collectives_tools(mcp: FastMCP):
    """Configure Nextcloud Collectives tools for the MCP server."""

    # --- Read Tools ---

    @mcp.tool(
        title="List Collectives",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:read")
    @instrument_tool
    async def collectives_get_collectives(
        ctx: Context,
    ) -> ListCollectivesResponse:
        """List all Nextcloud Collectives the user has access to"""
        client = await get_client(ctx)
        try:
            raw_collectives = await client.collectives.get_collectives()
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        collectives = [Collective(**c) for c in raw_collectives]
        return ListCollectivesResponse(collectives=collectives, total=len(collectives))

    @mcp.tool(
        title="List Collective Pages",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:read")
    @instrument_tool
    async def collectives_get_pages(
        ctx: Context, collective_id: int
    ) -> ListPagesResponse:
        """List all pages in a Nextcloud Collective

        Args:
            collective_id: ID of the collective
        """
        client = await get_client(ctx)
        try:
            raw_pages = await client.collectives.get_pages(collective_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        pages = [PageInfo(**p) for p in raw_pages]
        return ListPagesResponse(
            pages=pages, total=len(pages), collective_id=collective_id
        )

    @mcp.tool(
        title="Get Collective Page",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:read")
    @instrument_tool
    async def collectives_get_page(
        ctx: Context, collective_id: int, page_id: int
    ) -> GetPageResponse:
        """Get a page's metadata and markdown content from a Nextcloud Collective.

        Content is fetched via WebDAV using the page's file path. To update
        page content, use the nc_webdav_write_file tool with the path
        collectivePath/filePath/fileName (omit filePath for root-level pages).

        Args:
            collective_id: ID of the collective
            page_id: ID of the page
        """
        client = await get_client(ctx)
        try:
            raw_page = await client.collectives.get_page(collective_id, page_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        page = PageInfo(**raw_page)

        # Fetch content via WebDAV
        # Path structure: collectivePath/filePath/fileName
        # filePath is empty for root-level pages, contains subdirectory for nested pages
        content = None
        if page.collectivePath and page.fileName:
            parts = [page.collectivePath]
            if page.filePath:
                parts.append(page.filePath)
            parts.append(page.fileName)
            webdav_path = "/".join(p.strip("/") for p in parts)
            try:
                file_bytes, _ = await client.webdav.read_file(webdav_path)
                content = file_bytes.decode("utf-8")
            except (HTTPStatusError, OSError, UnicodeDecodeError) as e:
                logger.warning(
                    "Failed to read page content via WebDAV: %s: %s",
                    webdav_path,
                    e,
                )

        return GetPageResponse(page=page, content=content)

    @mcp.tool(
        title="Search Collective Pages",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:read")
    @instrument_tool
    async def collectives_search_pages(
        ctx: Context, collective_id: int, query: str
    ) -> SearchPagesResponse:
        """Full-text search within a Nextcloud Collective

        Args:
            collective_id: ID of the collective
            query: Search query string
        """
        client = await get_client(ctx)
        try:
            raw_pages = await client.collectives.search_pages(collective_id, query)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        pages = [PageInfo(**p) for p in raw_pages]
        return SearchPagesResponse(
            results=pages,
            total=len(pages),
            query=query,
            collective_id=collective_id,
        )

    @mcp.tool(
        title="List Collective Tags",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:read")
    @instrument_tool
    async def collectives_get_tags(
        ctx: Context, collective_id: int
    ) -> ListTagsResponse:
        """List all tags in a Nextcloud Collective

        Args:
            collective_id: ID of the collective
        """
        client = await get_client(ctx)
        try:
            raw_tags = await client.collectives.get_tags(collective_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        tags = [CollectiveTag(**t) for t in raw_tags]
        return ListTagsResponse(tags=tags, total=len(tags), collective_id=collective_id)

    @mcp.tool(
        title="List Trashed Collective Pages",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:read")
    @instrument_tool
    async def collectives_get_trashed_pages(
        ctx: Context, collective_id: int
    ) -> ListTrashedPagesResponse:
        """List trashed pages in a Nextcloud Collective

        Args:
            collective_id: ID of the collective
        """
        client = await get_client(ctx)
        try:
            raw_pages = await client.collectives.get_trashed_pages(collective_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        pages = [PageInfo(**p) for p in raw_pages]
        return ListTrashedPagesResponse(
            pages=pages, total=len(pages), collective_id=collective_id
        )

    # --- Write Tools ---

    @mcp.tool(
        title="Create Collective",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_create_collective(
        ctx: Context, name: str, emoji: str | None = None
    ) -> CreateCollectiveResponse:
        """Create a new Nextcloud Collective

        Args:
            name: Name of the collective
            emoji: Optional emoji for the collective
        """
        client = await get_client(ctx)
        try:
            raw = await client.collectives.create_collective(name, emoji)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        collective = Collective(**raw)
        return CreateCollectiveResponse(
            id=collective.id, name=collective.name, emoji=collective.emoji
        )

    @mcp.tool(
        title="Set Collective Emoji",
        annotations=ToolAnnotations(idempotentHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_set_collective_emoji(
        ctx: Context, collective_id: int, emoji: str
    ) -> CollectiveOperationResponse:
        """Set the emoji on a Nextcloud Collective.

        Setting the same emoji twice produces the same result (idempotent).

        Args:
            collective_id: ID of the collective
            emoji: Emoji to set on the collective
        """
        client = await get_client(ctx)
        try:
            raw = await client.collectives.update_collective(collective_id, emoji)
        except ValueError as e:
            raise McpError(ErrorData(code=400, message=str(e))) from e
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        collective = Collective(**raw)
        return CollectiveOperationResponse(
            collective_id=collective.id,
            status_code=200,
            message=f"Collective emoji set to: {collective.emoji}",
        )

    @mcp.tool(
        title="Trash Collective",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=False, openWorldHint=True
        ),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_trash_collective(
        ctx: Context, collective_id: int
    ) -> CollectiveOperationResponse:
        """Move a Nextcloud Collective to trash (soft delete).

        The collective can be restored or permanently deleted afterwards.

        Args:
            collective_id: ID of the collective to trash
        """
        client = await get_client(ctx)
        try:
            await client.collectives.trash_collective(collective_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        return CollectiveOperationResponse(
            collective_id=collective_id,
            status_code=200,
            message="Collective moved to trash",
        )

    @mcp.tool(
        title="Delete Collective",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_delete_collective(
        ctx: Context, collective_id: int
    ) -> CollectiveOperationResponse:
        """Permanently delete a Nextcloud Collective.

        WARNING: This is irreversible. The collective must be in the trash
        first (use collectives_trash_collective). All pages and content
        will be permanently destroyed.

        Args:
            collective_id: ID of the trashed collective to permanently delete
        """
        client = await get_client(ctx)
        try:
            await client.collectives.delete_collective(collective_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        return CollectiveOperationResponse(
            collective_id=collective_id,
            status_code=200,
            message="Collective permanently deleted",
        )

    @mcp.tool(
        title="Create Collective Page",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_create_page(
        ctx: Context, collective_id: int, parent_id: int, title: str
    ) -> CreatePageResponse:
        """Create a new page in a Nextcloud Collective.

        Pages are created as empty markdown files. Use nc_webdav_write_file
        with the path collectivePath/filePath/fileName to add content after
        creation (omit filePath for root-level pages).

        Args:
            collective_id: ID of the collective
            parent_id: ID of the parent page (use 0 for top-level pages)
            title: Title of the new page
        """
        client = await get_client(ctx)
        try:
            raw = await client.collectives.create_page(collective_id, parent_id, title)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        page = PageInfo(**raw)
        return CreatePageResponse(
            id=page.id,
            title=page.title,
            collective_id=collective_id,
            parent_id=page.parentId,
        )

    @mcp.tool(
        title="Move Collective Page",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_move_page(
        ctx: Context,
        collective_id: int,
        page_id: int,
        parent_id: int | None = None,
        title: str | None = None,
        index: int = 0,
        copy: bool = False,
    ) -> PageOperationResponse:
        """Move or copy a page within a Nextcloud Collective

        Args:
            collective_id: ID of the collective
            page_id: ID of the page to move/copy
            parent_id: Target parent page ID
            title: New title (optional)
            index: Position in subpage order (default 0)
            copy: If true, copy instead of move
        """
        client = await get_client(ctx)
        try:
            raw = await client.collectives.move_page(
                collective_id, page_id, parent_id, title, index, copy
            )
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        page = PageInfo(**raw)
        action = "copied" if copy else "moved"
        return PageOperationResponse(
            page_id=page.id,
            collective_id=collective_id,
            status_code=200,
            message=f"Page {action} (title: {page.title}, parent: {page.parentId})",
        )

    @mcp.tool(
        title="Trash Collective Page",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=False, openWorldHint=True
        ),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_trash_page(
        ctx: Context, collective_id: int, page_id: int
    ) -> PageOperationResponse:
        """Move a page to trash in a Nextcloud Collective (soft delete)

        Args:
            collective_id: ID of the collective
            page_id: ID of the page to trash
        """
        client = await get_client(ctx)
        try:
            await client.collectives.trash_page(collective_id, page_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        return PageOperationResponse(
            page_id=page_id,
            collective_id=collective_id,
            status_code=200,
            message="Page moved to trash",
        )

    @mcp.tool(
        title="Restore Collective Page",
        annotations=ToolAnnotations(idempotentHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_restore_page(
        ctx: Context, collective_id: int, page_id: int
    ) -> PageOperationResponse:
        """Restore a page from trash in a Nextcloud Collective

        Args:
            collective_id: ID of the collective
            page_id: ID of the page to restore
        """
        client = await get_client(ctx)
        try:
            raw = await client.collectives.restore_page(collective_id, page_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        page = PageInfo(**raw)
        return PageOperationResponse(
            page_id=page.id,
            collective_id=collective_id,
            status_code=200,
            message=f"Page restored from trash (title: {page.title})",
        )

    @mcp.tool(
        title="Set Collective Page Emoji",
        annotations=ToolAnnotations(idempotentHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_set_page_emoji(
        ctx: Context,
        collective_id: int,
        page_id: int,
        emoji: str | None = None,
    ) -> PageOperationResponse:
        """Set or clear the emoji on a Nextcloud Collective page

        Args:
            collective_id: ID of the collective
            page_id: ID of the page
            emoji: Emoji to set, or null to clear
        """
        client = await get_client(ctx)
        try:
            raw = await client.collectives.set_page_emoji(collective_id, page_id, emoji)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        page = PageInfo(**raw)
        return PageOperationResponse(
            page_id=page.id,
            collective_id=collective_id,
            status_code=200,
            message=f"Page emoji updated (emoji: {page.emoji})",
        )

    @mcp.tool(
        title="Create Collective Tag",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_create_tag(
        ctx: Context, collective_id: int, name: str, color: str
    ) -> CreateTagResponse:
        """Create a new tag in a Nextcloud Collective

        Args:
            collective_id: ID of the collective
            name: Tag name
            color: Hex color code (e.g. "FF0000")
        """
        client = await get_client(ctx)
        try:
            raw = await client.collectives.create_tag(collective_id, name, color)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        tag = CollectiveTag(**raw)
        return CreateTagResponse(id=tag.id, name=tag.name, color=tag.color)

    @mcp.tool(
        title="Assign Tag to Collective Page",
        annotations=ToolAnnotations(idempotentHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_assign_tag(
        ctx: Context, collective_id: int, page_id: int, tag_id: int
    ) -> PageOperationResponse:
        """Assign a tag to a page in a Nextcloud Collective

        Args:
            collective_id: ID of the collective
            page_id: ID of the page
            tag_id: ID of the tag to assign
        """
        client = await get_client(ctx)
        try:
            await client.collectives.assign_tag(collective_id, page_id, tag_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        return PageOperationResponse(
            page_id=page_id,
            collective_id=collective_id,
            status_code=200,
            message=f"Tag {tag_id} assigned to page",
        )

    @mcp.tool(
        title="Remove Tag from Collective Page",
        annotations=ToolAnnotations(idempotentHint=True, openWorldHint=True),
    )
    @require_scopes("collectives:write")
    @instrument_tool
    async def collectives_remove_tag(
        ctx: Context, collective_id: int, page_id: int, tag_id: int
    ) -> PageOperationResponse:
        """Remove a tag from a page in a Nextcloud Collective.

        This is a reversible operation — the tag still exists and can be
        reassigned with collectives_assign_tag.

        Args:
            collective_id: ID of the collective
            page_id: ID of the page
            tag_id: ID of the tag to remove
        """
        client = await get_client(ctx)
        try:
            await client.collectives.remove_tag(collective_id, page_id, tag_id)
        except (OCSError, HTTPStatusError) as e:
            raise _handle_collectives_error(e) from e
        return PageOperationResponse(
            page_id=page_id,
            collective_id=collective_id,
            status_code=200,
            message=f"Tag {tag_id} removed from page",
        )
