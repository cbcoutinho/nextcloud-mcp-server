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

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

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
    parse_board_document,
    summarize_board,
)
from nextcloud_client.models.whiteboard_edit import (
    MAX_ELEMENTS_PER_EDIT,
    BoardEditError,
    ElementChange,
    ElementId,
    NewElement,
    add_elements,
    delete_elements,
    serialize_board,
    update_elements,
)
from nextcloud_client.webdav import (
    OversizeDownload,
    UnexpectedContentType,
    normalize_etag,
)
from pydantic import Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.document_source import spool_target
from nextcloud_mcp_server.links import with_links
from nextcloud_mcp_server.models.webdav import FileInfo, SearchFilesResponse
from nextcloud_mcp_server.models.whiteboard import (
    WhiteboardEditResponse,
    WhiteboardResponse,
)
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

#: What a board saved by the app holds before anything is drawn on it.
NEW_BOARD: dict[str, Any] = {"elements": [], "scrollToContent": True}

T = TypeVar("T")


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
    client: "NextcloudClient", path: str, parse: Callable[[Path], T]
) -> tuple[T, str | None]:
    """Download a board and parse it in a worker thread. Returns what ``parse``
    returns, and the etag.

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
            return await run_sync(parse, target), etag
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


def _read_document(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    return parse_board_document(path.read_bytes())


async def _edit_board(
    client: "NextcloudClient",
    path: str,
    etag: str,
    edit: Callable[[dict[str, Any]], list[str]],
) -> WhiteboardEditResponse:
    """Read a board, apply ``edit`` to its scene, and save it, if and only if
    the board is still at ``etag``.

    The etag is checked twice: against the board as read, so that an edit is
    never computed on a newer board than the caller saw, and by the server on
    the write, so that a save between the read and the write is not lost.
    """
    expected = normalize_etag(etag)
    (document, scene), current = await _read_board(client, path, _read_document)
    if current is None or current != expected:
        raise _changed(path)

    def apply() -> tuple[list[str], bytes]:
        return edit(scene), serialize_board(document)

    try:
        ids, content = await run_sync(apply)
    except BoardEditError as e:
        raise ToolError(f"Nothing was changed: {e}.") from e
    _check_size(path, content)
    try:
        result = await client.webdav.write_file(
            path, content, WHITEBOARD_MIME_TYPE, if_match=current
        )
    except HTTPStatusError as e:
        raise _write_error(path, e) from e
    if result.get("status_code") == 412:
        raise _changed(path)
    if result.get("status_code") == 423:
        raise ToolError(f"{path!r} is locked by another client. Nothing was changed.")
    return WhiteboardEditResponse(path=path, etag=result.get("etag"), element_ids=ids)


def _check_size(path: str, content: bytes) -> None:
    """Refuse to save a board the tools could not read back, or larger than
    the operator allows for a write."""
    limits = [
        (
            MAX_BOARD_BYTES,
            f"{MAX_BOARD_BYTES // (1024 * 1024)} MB, the most these tools read",
        )
    ]
    max_mb = get_settings().webdav_write_max_mb
    if max_mb:
        limits.append((int(max_mb * 1024 * 1024), f"WEBDAV_WRITE_MAX_MB ({max_mb} MB)"))
    for limit, name in limits:
        if len(content) > limit:
            raise ToolError(
                f"{path!r} would grow to {len(content) / (1024 * 1024):.1f} MB, over "
                f"{name}. Nothing was changed."
            )


def _changed(path: str) -> ToolError:
    return ToolError(
        f"{path!r} has changed since that etag was read. Nothing was changed. "
        "Read it again with nc_whiteboard_get and redo the change on what it "
        "holds now."
    )


def _write_error(path: str, e: HTTPStatusError) -> ToolError:
    status = e.response.status_code
    if status == 403:
        return ToolError(f"You may not change {path!r}. Nothing was changed.")
    return ToolError(f"Failed to save {path!r}: HTTP {status}. Nothing was changed.")


async def _find_boards(
    client: "NextcloudClient", path: str, excluded: set[str]
) -> list[FileInfo]:
    """The boards under ``path``, without those in excluded folders."""
    try:
        found = await client.webdav.find_all_by_type(WHITEBOARD_MIME_TYPE, scope=path)
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ToolError(f"Folder not found: {path!r}") from e
        raise ToolError(
            f"Failed to list whiteboards: HTTP {e.response.status_code}"
        ) from e
    return [
        FileInfo(**item)
        for item in found
        if not is_path_excluded(item["path"], excluded)
    ]


def _refuse_bad_name(path: str) -> None:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if not name.lower().endswith(".whiteboard") or name.startswith("."):
        raise ToolError(f"A whiteboard's name must end in .whiteboard: {path!r}")


async def _create_board(client: "NextcloudClient", path: str) -> WhiteboardEditResponse:
    """Write an empty board, never over an existing file."""
    try:
        result = await client.webdav.write_file(
            path, serialize_board(NEW_BOARD), WHITEBOARD_MIME_TYPE
        )
    except HTTPStatusError as e:
        if e.response.status_code == 409:
            raise ToolError(f"Folder not found for {path!r}") from e
        raise _write_error(path, e) from e
    if result.get("status_code") == 412:
        raise ToolError(f"A file already exists at {path!r}")
    if result.get("status_code") == 423:
        raise ToolError(f"{path!r} is locked by another client")
    return WhiteboardEditResponse(path=path, etag=result.get("etag"), element_ids=[])


async def _edit(
    ctx: Context,
    path: str,
    etag: str,
    edit: Callable[[dict[str, Any]], list[str]],
) -> WhiteboardEditResponse:
    """Check the path, then edit the board through ``_edit_board``."""
    _refuse_traversal(path)
    client = await get_client(ctx)
    excluded = await get_excluded_file_paths(client.webdav)
    _refuse_excluded(path, excluded)
    return await _edit_board(client, path, etag, edit)


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
        boards = await _find_boards(client, path, excluded)
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
        elements, etag = await _read_board(client, path, _read_elements)
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

    @mcp.tool(
        title="Create Whiteboard",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("whiteboard.write")
    @instrument_tool
    async def nc_whiteboard_create(path: str, ctx: Context) -> WhiteboardEditResponse:
        """Create an empty Nextcloud whiteboard.

        Fails if a file already exists at the path. Add content with
        nc_whiteboard_add_elements, passing the etag this returns.

        Args:
            path: Path of the new board. The name must end in .whiteboard, and
                the folder must exist.
        """
        _refuse_traversal(path)
        _refuse_bad_name(path)
        client = await get_client(ctx)
        excluded = await get_excluded_file_paths(client.webdav)
        _refuse_excluded(path, excluded)
        return await _create_board(client, path)

    @mcp.tool(
        title="Add Whiteboard Elements",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("whiteboard.write")
    @instrument_tool
    async def nc_whiteboard_add_elements(
        path: str,
        etag: str,
        elements: Annotated[
            list[NewElement], Field(min_length=1, max_length=MAX_ELEMENTS_PER_EDIT)
        ],
        ctx: Context,
    ) -> WhiteboardEditResponse:
        """Add shapes, texts, arrows and frames to a Nextcloud whiteboard.

        A shape takes its label as text. An arrow connects two elements by id:
        existing ones, or ones added in the same call with an id given here.
        Labels and arrow ends are placed for you. Coordinates grow to the right
        and downwards, and nc_whiteboard_get shows where things already are.
        Each call reads the whole board and saves it again.

        Warning: if someone has the board open in a browser, their next save
        can silently undo this change. Ask the user to close the board first.

        Args:
            path: Path of the board
            etag: The etag from the last nc_whiteboard_get, or from the last
                change. The change is refused if the board changed since
            elements: The elements to add, in order. Later ones are drawn on top

        Returns:
            The new etag, and the ids of the added elements in the order given.
        """
        return await _edit(ctx, path, etag, lambda scene: add_elements(scene, elements))

    @mcp.tool(
        title="Update Whiteboard Elements",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("whiteboard.write")
    @instrument_tool
    async def nc_whiteboard_update_elements(
        path: str,
        etag: str,
        changes: Annotated[
            list[ElementChange], Field(min_length=1, max_length=MAX_ELEMENTS_PER_EDIT)
        ],
        ctx: Context,
    ) -> WhiteboardEditResponse:
        """Change the text, position, size or colours of elements on a whiteboard.

        Elements are named by their id from nc_whiteboard_get. A shape moves
        with its label, a frame with what is in it, and arrows follow the
        elements they connect. An element moved out of its frame leaves it.
        Each call reads the whole board and saves it again.

        Warning: if someone has the board open in a browser, their next save
        can silently undo this change. Ask the user to close the board first.

        Args:
            path: Path of the board
            etag: The etag from the last nc_whiteboard_get, or from the last
                change. The change is refused if the board changed since
            changes: One entry per element. Fields left out stay as they are

        Returns:
            The new etag, and the ids of every element that changed, including
            labels and arrows that followed.
        """
        return await _edit(
            ctx, path, etag, lambda scene: update_elements(scene, changes)
        )

    @mcp.tool(
        title="Delete Whiteboard Elements",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )
    @require_scopes("whiteboard.write")
    @instrument_tool
    async def nc_whiteboard_delete_elements(
        path: str,
        etag: str,
        ids: Annotated[
            list[ElementId], Field(min_length=1, max_length=MAX_ELEMENTS_PER_EDIT)
        ],
        ctx: Context,
    ) -> WhiteboardEditResponse:
        """Delete elements from a Nextcloud whiteboard.

        A shape's label goes with it. An arrow that ends at a deleted element
        stays, unattached at that end. What is in a deleted frame stays.
        Nextcloud keeps earlier versions of the file, so a deletion can be
        undone by restoring one. Each call reads the whole board and saves it
        again.

        Warning: if someone has the board open in a browser, their next save
        can silently undo this change. Ask the user to close the board first.

        Args:
            path: Path of the board
            etag: The etag from the last nc_whiteboard_get, or from the last
                change. The change is refused if the board changed since
            ids: Ids of the elements to delete, from nc_whiteboard_get

        Returns:
            The new etag, and the ids of every element that changed.
        """
        return await _edit(ctx, path, etag, lambda scene: delete_elements(scene, ids))
