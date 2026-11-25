"""MCP tools for the Nextcloud Whiteboard app.

A board is a file, so these tools read it over WebDAV and need nothing from the
app itself, which publishes no capability to gate on. What they add over the
file tools is an understanding of the format: ``nc_webdav_read_file`` returns a
board as base64, most of it styling and embedded images.

They read files under their own scope, without ``files.read``. Two rules follow
from that: a board is recognised by the content type Nextcloud reports, never
by what the caller asks for, and ``EXCLUDED_TAGS`` applies as it does to the
file tools.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from anyio.to_thread import run_sync
from httpx2 import HTTPStatusError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from nextcloud_client.models.whiteboard import (
    WHITEBOARD_MIME_TYPE,
    InvalidBoardError,
    WhiteboardElement,
    parse_board,
    summarize_board,
)
from nextcloud_client.webdav import OversizeDownload, UnexpectedContentType
from pydantic import Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.document_source import spool_target
from nextcloud_mcp_server.links import with_links
from nextcloud_mcp_server.models.webdav import FileInfo, SearchFilesResponse
from nextcloud_mcp_server.models.whiteboard import WhiteboardResponse
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.server.tag_exclusion import (
    get_excluded_file_paths,
    is_path_excluded,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, as in server/webdav.py
    from nextcloud_client import NextcloudClient

#: Cap on the board file. Images are embedded in it as base64, so a real board
#: can be many megabytes; parsing needs several times that in memory.
MAX_BOARD_BYTES = 40 * 1024 * 1024

#: Elements per response, at most and by default.
MAX_ELEMENTS = 1000
DEFAULT_ELEMENTS = 300

#: Characters of serialized elements per response. A page ends early rather than
#: exceed it, but always holds one element, whose size the model bounds.
MAX_PAGE_CHARS = 100_000


def _read_elements(path: Path) -> list[WhiteboardElement]:
    """Parse the downloaded board. Blocking, so it runs in a worker thread."""
    return summarize_board(parse_board(path.read_bytes()))


def _page(
    elements: list[WhiteboardElement], offset: int, limit: int
) -> list[WhiteboardElement]:
    """The elements from ``offset`` that fit one response."""
    page: list[WhiteboardElement] = []
    chars = 0
    for element in elements[offset : offset + limit]:
        chars += len(element.model_dump_json())
        if page and chars > MAX_PAGE_CHARS:
            break
        page.append(element)
    return page


def _refuse_traversal(path: str) -> None:
    """Refuse ``..`` with a message the model can act on."""
    if ".." in path.replace("\\", "/").split("/"):
        raise ToolError(f"Path may not contain '..' segments: {path!r}")


def _refuse_excluded(path: str, excluded: set[str]) -> None:
    if is_path_excluded(path, excluded):
        raise ToolError(f"Access denied: {path!r} is tagged with an excluded tag")


async def _read_board(
    client: "NextcloudClient", path: str
) -> tuple[list[WhiteboardElement], str | None]:
    """Download a board and summarize it. Returns its elements and etag.

    A token may hold ``whiteboard.read`` without ``files.read``, so this must say
    nothing about other files. The content type is checked before the body is
    read, and a missing file, another kind of file and one that is too large all
    get the same message.
    """
    try:
        with spool_target(get_settings().document_spool_dir) as target:
            _, _, etag = await client.webdav.stream_to_file(
                path,
                target,
                max_bytes=MAX_BOARD_BYTES,
                expected_type=WHITEBOARD_MIME_TYPE,
            )
            return await run_sync(_read_elements, target), etag
    except (OversizeDownload, UnexpectedContentType) as e:
        raise _no_board(path) from e
    except InvalidBoardError as e:
        raise ToolError(f"{path!r} is not a readable whiteboard: {e}") from e
    except HTTPStatusError as e:
        if e.response.status_code in (403, 404):
            raise _no_board(path) from e
        raise ToolError(
            f"Failed to read {path!r}: HTTP {e.response.status_code}"
        ) from e


def _no_board(path: str) -> ToolError:
    return ToolError(
        f"No whiteboard to read at {path!r}: it is missing, not accessible, not a "
        f"whiteboard, or over {MAX_BOARD_BYTES // (1024 * 1024)} MB."
    )


def configure_whiteboard_tools(mcp: MCPServer) -> None:
    """Configure Whiteboard app MCP tools."""

    @mcp.tool(
        title="List Whiteboards",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("whiteboard.read")
    @with_links
    @instrument_tool
    async def nc_whiteboard_list(ctx: Context, path: str = "") -> SearchFilesResponse:
        """List the Nextcloud whiteboards in a folder and everything below it.

        Board templates are listed too.

        Args:
            path: Folder to search in (empty string for all of the user's files)

        Returns:
            SearchFilesResponse with one entry per board
        """
        _refuse_traversal(path)
        client = await get_client(ctx)
        excluded = await get_excluded_file_paths(client.webdav)
        if path:
            _refuse_excluded(path, excluded)
        try:
            found = await client.webdav.find_all_by_type(
                WHITEBOARD_MIME_TYPE, scope=path
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ToolError(f"Folder not found: {path!r}") from e
            raise ToolError(
                f"Failed to list whiteboards: HTTP {e.response.status_code}"
            ) from e
        boards = [
            FileInfo(**item)
            for item in found
            if not is_path_excluded(item["path"], excluded)
        ]
        return SearchFilesResponse(
            results=boards,
            total_found=len(boards),
            scope=path,
            filters_applied={"mime_type": WHITEBOARD_MIME_TYPE},
        )

    @mcp.tool(
        title="Get Whiteboard",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("whiteboard.read")
    @instrument_tool
    async def nc_whiteboard_get(
        path: str,
        ctx: Context,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_ELEMENTS)] = DEFAULT_ELEMENTS,
    ) -> WhiteboardResponse:
        """Read what is on a Nextcloud whiteboard: its shapes, texts and arrows.

        Returns a compact view. Each element has its position and size, its
        text (a label is the text of its shape or arrow) and, for an arrow, the
        ids of the elements it connects. Styling, image content and deleted
        elements are left out. Use this rather than nc_webdav_read_file, which
        returns a board as base64.

        This is the last saved state. While someone has the board open, their
        most recent changes may not be saved yet. A board over 40 MB cannot be
        read.

        Every call reads the file again. When reading several pages, compare
        ``etag``: if it changed, the board was saved in between and elements may
        be missing or repeated, so start again from offset 0.

        Args:
            path: Path of the board, from nc_whiteboard_list
            offset: Number of elements to skip. Pass ``next_offset`` from the
                previous response to read on
            limit: Maximum number of elements to return. A page with a lot of
                text holds fewer
        """
        _refuse_traversal(path)
        client = await get_client(ctx)
        excluded = await get_excluded_file_paths(client.webdav)
        _refuse_excluded(path, excluded)
        elements, etag = await _read_board(client, path)
        page = _page(elements, offset, limit)
        end = offset + len(page)
        return WhiteboardResponse(
            path=path,
            etag=etag,
            elements=page,
            total_count=len(elements),
            offset=offset,
            next_offset=end if end < len(elements) else None,
        )
