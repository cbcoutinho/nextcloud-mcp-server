"""MCP tool that shows an image file to a vision model.

Every other file tool returns text. ``nc_webdav_read_file`` hands an image back
as base64 inside its JSON (or as OCR text), which a model cannot look at: to
the model it is just a very long string. This tool returns an MCP image block
instead, which clients pass to the model as an image, plus the usual typed
response as structured content.

Kept apart from ``server/webdav.py`` because it brings its own concerns
(Pillow, image content blocks); it registers as part of the WebDAV tools.
"""

import base64
import json
import logging
import mimetypes
from typing import TYPE_CHECKING, Annotated, Literal

from anyio.to_thread import run_sync
from httpx import HTTPStatusError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from nextcloud_mcp_server.astrolabe_links import astrolabe_browser_base
from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client.webdav import OversizeDownload
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.links import file_url
from nextcloud_mcp_server.models.webdav import ViewImageResponse
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.server.tag_exclusion import (
    get_excluded_file_paths,
    is_path_excluded,
)
from nextcloud_mcp_server.utils.image_view import (
    DEFAULT_MAX_SIZE,
    MAX_SIZE,
    ImageError,
    PreparedImage,
    prepare_image,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, as in server/webdav.py
    from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

#: Preview sizes Nextcloud renders (it rounds a request up to the next one).
#: The last is the largest preview it allows by default.
_PREVIEW_STEPS = (256, 1024, 4096)

#: Cap on the original file, downloaded only when Nextcloud has no preview.
#: Generous for photos, small enough that a mislabelled video is not pulled in.
ORIGINAL_MAX_BYTES = 40 * 1024 * 1024


def _preview_step(max_size: int) -> int:
    """The preview size to request for an image of at most ``max_size`` pixels.

    Strictly above ``max_size``, so that ``source_width`` equal to ``width``
    in the response really means the image has no more detail to offer.
    """
    return next(
        (step for step in _PREVIEW_STEPS if step > max_size), _PREVIEW_STEPS[-1]
    )


async def _fetch_original(client: "NextcloudClient", path: str) -> bytes:
    """Download the original file, for when Nextcloud renders no preview."""
    # Lazily, like nc_webdav_read_file: the spool helpers live in the ingest
    # layer, which server startup must not load.
    from nextcloud_mcp_server.vector.spool import spooled_document  # noqa: PLC0415

    try:
        async with spooled_document(
            client,
            path,
            spool_dir=get_settings().document_spool_dir,
            max_bytes=ORIGINAL_MAX_BYTES,
        ) as source:

            def _read_all() -> bytes:
                with source.open() as fh:
                    return fh.read()

            return await run_sync(_read_all)
    except OversizeDownload as e:
        raise ToolError(
            f"Nextcloud has no preview of {path!r}, and the original is larger "
            f"than {ORIGINAL_MAX_BYTES // (1024 * 1024)} MB."
        ) from e
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ToolError(f"File not found: {path!r}") from e
        raise ToolError(
            f"Failed to download {path!r}: HTTP {e.response.status_code}"
        ) from e


async def _file_link(client: "NextcloudClient", path: str) -> str | None:
    """Files-app link to ``path``, or None. Fail-open, as in nc_webdav_read_file:
    the link is a convenience, the image is the tool's job."""
    browser_base = astrolabe_browser_base()
    if not browser_base:
        return None
    try:
        return file_url(browser_base, await client.webdav.get_fileid(path))
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.debug("No file link for %r: fileid lookup failed: %s", path, e)
        return None


@require_scopes("files.read")
@instrument_tool
async def nc_webdav_view_image(
    path: str,
    ctx: Context,
    max_size: Annotated[int, Field(ge=64, le=MAX_SIZE)] = DEFAULT_MAX_SIZE,
) -> Annotated[CallToolResult, ViewImageResponse]:
    """Show an image file so you can look at it (photos, screenshots, scans).

    Returns the image itself, which a vision model sees as a picture, not as
    text. Use this instead of nc_webdav_read_file for images: that tool
    returns them as base64 text or OCR output, which you cannot look at.
    Works for images anywhere in the user's files, including Notes
    attachments, images on Collectives pages and files shared in Talk.

    The image is scaled down to at most ``max_size`` pixels on its longer
    edge, the right size for a vision model. Raise it (up to 2576) to read
    small print, lower it to save tokens. A photo is turned upright according
    to its orientation data.

    Nextcloud renders the image, so formats such as HEIC work when the
    Nextcloud server can preview them. Otherwise the original is decoded here
    (JPEG, PNG, GIF, WebP, BMP, TIFF). Files that are not images are refused.
    Raises ``ToolError`` for those, for files that do not exist or cannot be
    decoded, and when ``EXCLUDED_TAGS`` hides the file.

    Args:
        path: Path of the image, relative to the user's files root
        max_size: Longest edge of the image you receive, in pixels (64-2576)

    Returns:
        The image, followed by ``ViewImageResponse``: its size and format,
        whether it was made from Nextcloud's preview or the original, and the
        size of what it was made from (larger than the image you got means a
        higher ``max_size`` would show more detail).
    """
    guessed, _ = mimetypes.guess_type(path)
    if guessed is not None and not guessed.startswith("image/"):
        # Nextcloud would happily render a text file or a PDF as a picture,
        # which is not what anyone viewing it as an image wants.
        raise ToolError(
            f"{path!r} is not an image ({guessed}). Use nc_webdav_read_file to read it."
        )

    client = await get_client(ctx)
    excluded = await get_excluded_file_paths(client.webdav)
    if is_path_excluded(path, excluded):
        raise ToolError(f"Access denied: {path!r} is tagged with an excluded tag")

    source: Literal["preview", "original"] = "preview"
    try:
        raw, _ = await client.webdav.get_preview(path, _preview_step(max_size))
    except HTTPStatusError as e:
        if e.response.status_code != 404:
            raise ToolError(
                f"Failed to load a preview of {path!r}: HTTP {e.response.status_code}"
            ) from e
        # 404: no such file, or no preview provider for its type. The
        # download tells the two apart.
        logger.debug("No preview for %r, falling back to the original", path)
        source = "original"
        raw = await _fetch_original(client, path)

    try:
        prepared: PreparedImage = await run_sync(prepare_image, raw, max_size)
    except ImageError as e:
        raise ToolError(f"Cannot show {path!r}: {e}.") from e

    response = ViewImageResponse(
        path=path,
        mime_type=prepared.mime_type,
        width=prepared.width,
        height=prepared.height,
        source=source,
        source_width=prepared.source_width,
        source_height=prepared.source_height,
        size=len(prepared.data),
        url=await _file_link(client, path),
    )
    structured = response.model_dump(mode="json", by_alias=True)
    return CallToolResult(
        content=[
            ImageContent(
                type="image",
                data=base64.b64encode(prepared.data).decode("ascii"),
                mime_type=prepared.mime_type,
            ),
            TextContent(type="text", text=json.dumps(structured, indent=2)),
        ],
        structured_content=structured,
    )


def configure_webdav_image_tools(mcp: MCPServer) -> None:
    """Register the image tool. Called from ``configure_webdav_tools``."""
    mcp.tool(
        title="View Image",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_webdav_view_image)
