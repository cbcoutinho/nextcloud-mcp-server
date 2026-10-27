"""Server-layer tests for ``nc_webdav_view_image`` and the preview client call.

The image processing itself is covered in ``test_image_view.py``. These pin
the wiring: which Nextcloud preview is requested, when the original is
downloaded instead, what is refused before any request, and that the result
reaches the client as an image block plus a structured response the SDK
validates against ``ViewImageResponse``.
"""

import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent
from PIL import Image

from nextcloud_mcp_server.client.webdav import OversizeDownload, WebDAVClient
from nextcloud_mcp_server.models.webdav import ViewImageResponse
from nextcloud_mcp_server.server.webdav import configure_webdav_tools
from nextcloud_mcp_server.server.webdav_image import nc_webdav_view_image
from tests.client.conftest import create_mock_response

pytestmark = pytest.mark.unit

_MODULE = "nextcloud_mcp_server.server.webdav_image"


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path (no transport,
    no verified token), as in ``test_webdav_tools_exclusion.py``."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


def _jpeg(size=(4000, 3000)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (20, 100, 180)).save(out, format="JPEG")
    return out.getvalue()


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://test.local/index.php/core/preview.png")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


@pytest.fixture
def webdav(mocker):
    """A fake WebDAV client: a preview and an original, both configurable."""
    state = {"preview": _jpeg(), "original": _jpeg(), "excluded": set()}
    client = SimpleNamespace(webdav=AsyncMock())

    async def get_preview(path, size):
        if isinstance(state["preview"], Exception):
            raise state["preview"]
        return state["preview"], "image/jpeg"

    async def stream_to_file(path, dest, *, max_bytes=None):
        await anyio.lowlevel.checkpoint()
        body = state["original"]
        if isinstance(body, Exception):
            raise body
        if max_bytes is not None and len(body) > max_bytes:
            raise OversizeDownload(f"{path} exceeded {max_bytes} bytes")
        dest.write_bytes(body)
        return len(body), "image/jpeg", "etag-1"

    client.webdav.get_preview = AsyncMock(side_effect=get_preview)
    client.webdav.stream_to_file = AsyncMock(side_effect=stream_to_file)

    async def fake_get_client(ctx):
        return client

    async def excluded(*_, **__):
        return state["excluded"]

    mocker.patch(f"{_MODULE}.get_client", side_effect=fake_get_client)
    mocker.patch(f"{_MODULE}.get_excluded_file_paths", side_effect=excluded)
    mocker.patch(
        f"{_MODULE}.get_settings",
        return_value=SimpleNamespace(document_spool_dir=None),
    )
    mocker.patch(f"{_MODULE}.astrolabe_browser_base", return_value=None)
    state["client"] = client
    return state


@pytest.fixture
def mcp() -> MCPServer:
    server = MCPServer(name="test-view-image")
    configure_webdav_tools(server)
    return server


def _ctx() -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.request_context = SimpleNamespace()
    return ctx


# ── Result shape, through the SDK ───────────────────────────────────────


async def test_result_is_an_image_block_plus_validated_structured_content(mcp, webdav):
    """Called through the SDK, so the structured part is validated against
    ViewImageResponse exactly as it is for a real client."""
    result = await mcp.call_tool("nc_webdav_view_image", {"path": "Photos/a.jpg"})

    image, text = result.content
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/jpeg"
    assert Image.open(io.BytesIO(base64.b64decode(image.data))).size == (1568, 1176)
    assert isinstance(text, TextContent)
    assert json.loads(text.text)["path"] == "Photos/a.jpg"

    data = ViewImageResponse.model_validate(result.structured_content)
    assert (data.width, data.height) == (1568, 1176)
    assert (data.source_width, data.source_height) == (4000, 3000)
    assert data.source == "preview"
    assert data.size == len(base64.b64decode(image.data))


@pytest.mark.parametrize("max_size", [63, 2577])
async def test_max_size_outside_the_range_is_refused(mcp, webdav, max_size):
    """Above 2576 the Claude API downscales anyway, so the schema stops it."""
    with pytest.raises(ToolError, match="max_size"):
        await mcp.call_tool(
            "nc_webdav_view_image", {"path": "a.jpg", "max_size": max_size}
        )

    webdav["client"].webdav.get_preview.assert_not_awaited()


# ── Which preview, and when the original ────────────────────────────────


@pytest.mark.parametrize(
    ("max_size", "step"),
    [(64, 256), (800, 1024), (1024, 4096), (1568, 4096), (2576, 4096)],
)
async def test_preview_size_is_the_next_step_above_max_size(webdav, max_size, step):
    """Strictly above, so source_width == width means there is no more detail."""
    await nc_webdav_view_image(path="a.jpg", ctx=_ctx(), max_size=max_size)

    webdav["client"].webdav.get_preview.assert_awaited_once_with("a.jpg", step)


async def test_preview_is_used_without_downloading_the_original(webdav):
    await nc_webdav_view_image(path="a.jpg", ctx=_ctx())

    webdav["client"].webdav.stream_to_file.assert_not_awaited()


async def test_missing_preview_falls_back_to_the_original(webdav):
    """404: no preview provider for the type. The original is decoded here."""
    webdav["preview"] = _http_error(404)

    result = await nc_webdav_view_image(path="a.webp", ctx=_ctx())

    assert result.structured_content["source"] == "original"
    webdav["client"].webdav.stream_to_file.assert_awaited_once()


async def test_missing_file_is_reported_as_not_found(webdav):
    webdav["preview"] = _http_error(404)
    webdav["original"] = _http_error(404)
    ctx = _ctx()

    with pytest.raises(ToolError, match="File not found"):
        await nc_webdav_view_image(path="gone.jpg", ctx=ctx)


async def test_other_preview_errors_do_not_fall_back(webdav):
    webdav["preview"] = _http_error(500)
    ctx = _ctx()

    with pytest.raises(ToolError, match="HTTP 500"):
        await nc_webdav_view_image(path="a.jpg", ctx=ctx)

    webdav["client"].webdav.stream_to_file.assert_not_awaited()


async def test_oversized_original_is_refused(webdav, mocker):
    webdav["preview"] = _http_error(404)
    mocker.patch(f"{_MODULE}.ORIGINAL_MAX_BYTES", 10)
    ctx = _ctx()

    with pytest.raises(ToolError, match="no preview"):
        await nc_webdav_view_image(path="huge.tif", ctx=ctx)


async def test_undecodable_original_is_refused(webdav):
    """E.g. HEIC on a server that cannot preview it: Pillow cannot read it."""
    webdav["preview"] = _http_error(404)
    webdav["original"] = b"ftypheic not decodable here"
    ctx = _ctx()

    with pytest.raises(ToolError, match="Cannot show 'IMG_1.heic'"):
        await nc_webdav_view_image(path="IMG_1.heic", ctx=ctx)


# ── Refused before any request ──────────────────────────────────────────


@pytest.mark.parametrize("path", ["notes.txt", "report.pdf", "Docs/page.md"])
async def test_files_that_are_not_images_are_refused(webdav, path):
    """Nextcloud would render these as pictures, which nobody asked for."""
    ctx = _ctx()

    with pytest.raises(ToolError, match="is not an image"):
        await nc_webdav_view_image(path=path, ctx=ctx)

    webdav["client"].webdav.get_preview.assert_not_awaited()


async def test_file_without_extension_is_tried(webdav):
    """No extension, no verdict: let Nextcloud and Pillow decide."""
    result = await nc_webdav_view_image(path="Scans/scan0001", ctx=_ctx())

    assert result.structured_content["mime_type"] == "image/jpeg"


async def test_excluded_path_is_not_requested(webdav):
    webdav["excluded"] = {"Private/me.jpg"}
    ctx = _ctx()

    with pytest.raises(ToolError, match="excluded tag"):
        await nc_webdav_view_image(path="/Private/me.jpg", ctx=ctx)

    webdav["client"].webdav.get_preview.assert_not_awaited()


# ── Link ────────────────────────────────────────────────────────────────


async def test_link_is_added_when_a_browser_base_is_configured(webdav, mocker):
    mocker.patch(f"{_MODULE}.astrolabe_browser_base", return_value="https://nc.example")
    webdav["client"].webdav.get_fileid = AsyncMock(return_value="42")

    result = await nc_webdav_view_image(path="a.jpg", ctx=_ctx())

    assert result.structured_content["url"] == "https://nc.example/index.php/f/42"


async def test_link_lookup_failure_does_not_cost_the_image(webdav, mocker):
    mocker.patch(f"{_MODULE}.astrolabe_browser_base", return_value="https://nc.example")
    webdav["client"].webdav.get_fileid = AsyncMock(side_effect=_http_error(403))

    result = await nc_webdav_view_image(path="a.jpg", ctx=_ctx())

    assert result.structured_content["url"] is None
    assert isinstance(result.content[0], ImageContent)


# ── Registration ────────────────────────────────────────────────────────


def test_registration(mcp):
    tool = {t.name: t for t in mcp._tool_manager.list_tools()}["nc_webdav_view_image"]

    assert tool.title == "View Image"
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is True
    assert list(tool.fn._required_scopes) == ["files.read"]
    assert set(tool.output_schema["properties"]) >= {"path", "width", "source"}


def test_read_file_points_to_the_image_tool(mcp):
    """Agents pick tools by description: the base64 path names the better one."""
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert "nc_webdav_view_image" in tools["nc_webdav_read_file"].description


# ── Client ──────────────────────────────────────────────────────────────


async def test_get_preview_requests_the_core_preview_route(mocker):
    """Bare /core/ path (the base class adds /index.php), no icon fallback."""
    mock_make_request = mocker.patch.object(
        WebDAVClient,
        "_make_request",
        return_value=create_mock_response(
            content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}
        ),
    )

    client = WebDAVClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    content, content_type = await client.get_preview("Photos/a b.jpg", 1024)

    assert (content, content_type) == (b"\xff\xd8jpeg", "image/jpeg")
    call = mock_make_request.call_args
    assert call.args == ("GET", "/core/preview.png")
    assert call.kwargs["params"] == {
        "file": "/Photos/a b.jpg",
        "x": 1024,
        "y": 1024,
        "a": 1,
        "forceIcon": 0,
        "mimeFallback": "false",
    }
