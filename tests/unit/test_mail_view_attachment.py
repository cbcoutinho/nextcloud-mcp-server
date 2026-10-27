"""Server-layer tests for ``nc_mail_view_attachment``.

The image processing itself is covered in ``test_image_view.py`` and the
download in ``tests/client/mail/test_mail_api.py``. These pin the wiring: what
is refused before any decoding, how download failures are reported, and that
the result reaches the client as an image block plus a structured response the
SDK validates against ``ViewMailAttachmentResponse``.

How a failure surfaces depends on the server class. A bare ``MCPServer`` passes
the ``MCPError`` this module raises out of ``call_tool`` unchanged (mcp 2.x puts
it on the wire as a JSON-RPC error). ``NextcloudMCPServer``, which production
runs, maps it back to a tool result with ``is_error=True`` that the model reads.
Both are asserted, since either alone would hide the other.
"""

import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import ImageContent, TextContent
from PIL import Image

from nextcloud_mcp_server.errors import NextcloudMCPServer
from nextcloud_mcp_server.models.mail import ViewMailAttachmentResponse
from nextcloud_mcp_server.server.mail import configure_mail_tools
from nextcloud_mcp_server.utils.image_view import prepare_image

pytestmark = pytest.mark.unit

_MODULE = "nextcloud_mcp_server.server.mail"
_ARGS = {"message_id": 100, "attachment_id": "1.2"}


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path (no transport,
    no verified token), as in ``test_mail_write_tools.py``."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


def _encode(image: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


def _jpeg(size: tuple[int, int] = (4000, 3000)) -> bytes:
    return _encode(Image.new("RGB", size, (20, 100, 180)), "JPEG")


def _transparent_png() -> bytes:
    return _encode(Image.new("RGBA", (300, 200), (255, 0, 0, 0)), "PNG")


def _request_error() -> httpx.RequestError:
    return httpx.ConnectError(
        "connection refused",
        request=httpx.Request("GET", "http://test.local/attachment"),
    )


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://test.local/attachment")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


@pytest.fixture
def mail(mocker):
    """A fake Mail client. ``state["attachment"]`` is what the download returns
    (``(content, mime, name)``), or an exception to raise instead."""
    state: dict = {"attachment": (_jpeg(), "image/jpeg", "photo.jpg")}
    client = SimpleNamespace(mail=AsyncMock())

    async def download_attachment(message_id, attachment_id):
        if isinstance(state["attachment"], Exception):
            raise state["attachment"]
        return state["attachment"]

    client.mail.download_attachment = AsyncMock(side_effect=download_attachment)

    async def fake_get_client(ctx):
        return client

    mocker.patch(f"{_MODULE}.get_client", side_effect=fake_get_client)
    state["client"] = client
    return state


@pytest.fixture
def prepare(mocker):
    """The decoder, wrapped: it still runs, and a test can ask whether it did."""
    return mocker.patch(f"{_MODULE}.prepare_image", wraps=prepare_image)


@pytest.fixture
def mcp() -> MCPServer:
    server = MCPServer(name="test-mail-view-attachment")
    configure_mail_tools(server)
    return server


# ── Result shape, through the SDK ───────────────────────────────────────


async def test_result_is_an_image_block_plus_validated_structured_content(mcp, mail):
    """Called through the SDK, so the structured part is validated against
    ViewMailAttachmentResponse exactly as it is for a real client."""
    result = await mcp.call_tool("nc_mail_view_attachment", _ARGS)

    image, text = result.content
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/jpeg"
    assert Image.open(io.BytesIO(base64.b64decode(image.data))).size == (1568, 1176)
    assert isinstance(text, TextContent)
    assert json.loads(text.text)["name"] == "photo.jpg"

    data = ViewMailAttachmentResponse.model_validate(result.structured_content)
    assert (data.message_id, data.attachment_id) == (100, "1.2")
    assert data.name == "photo.jpg"
    assert data.mime_type == "image/jpeg"
    assert (data.width, data.height) == (1568, 1176)
    assert (data.source_width, data.source_height) == (4000, 3000)
    assert data.size == len(base64.b64decode(image.data))
    mail["client"].mail.download_attachment.assert_awaited_once_with(100, "1.2")


async def test_max_size_bounds_the_image(mcp, mail):
    result = await mcp.call_tool("nc_mail_view_attachment", {**_ARGS, "max_size": 512})

    data = ViewMailAttachmentResponse.model_validate(result.structured_content)
    assert (data.width, data.height) == (512, 384)
    assert (data.source_width, data.source_height) == (4000, 3000)


async def test_transparent_png_stays_png(mcp, mail):
    mail["attachment"] = (_transparent_png(), "image/png", "logo.png")

    result = await mcp.call_tool("nc_mail_view_attachment", _ARGS)

    assert result.content[0].mime_type == "image/png"
    assert result.structured_content["mime_type"] == "image/png"
    assert (
        result.structured_content["width"],
        result.structured_content["height"],
    ) == (
        300,
        200,
    )


@pytest.mark.parametrize("max_size", [63, 2577])
async def test_max_size_outside_the_range_is_refused(mcp, mail, max_size):
    """Above 2576 the Claude API downscales anyway, so the schema stops it."""
    with pytest.raises(ToolError, match="max_size"):
        await mcp.call_tool("nc_mail_view_attachment", {**_ARGS, "max_size": max_size})

    mail["client"].mail.download_attachment.assert_not_awaited()


# ── What counts as an image ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mime", "name"),
    [
        ("image/jpeg", "photo.jpg"),
        ("IMAGE/JPEG", "photo.jpg"),
        # The type is the verdict when it names an image, whatever the name says.
        ("image/jpeg", "photo.bin"),
        # A generic type says nothing, so the name decides: an image guess...
        ("application/octet-stream", "photo.jpeg"),
        ("", "photo.jpeg"),
        # ...or no guess at all is tried, and Pillow settles it.
        ("application/octet-stream", "scan0001"),
        ("application/octet-stream", "attachment_100_1.2"),
    ],
)
async def test_attachments_that_may_be_images_are_decoded(mcp, mail, mime, name):
    mail["attachment"] = (_jpeg((800, 600)), mime, name)

    result = await mcp.call_tool("nc_mail_view_attachment", _ARGS)

    assert isinstance(result.content[0], ImageContent)
    assert result.structured_content["name"] == name


async def test_octet_stream_with_an_image_extension_is_shown(mcp, mail):
    """Mail clients attach unknown files as octet-stream, whatever they are."""
    mail["attachment"] = (_transparent_png(), "application/octet-stream", "logo.png")

    result = await mcp.call_tool("nc_mail_view_attachment", _ARGS)

    assert result.content[0].mime_type == "image/png"
    assert result.structured_content["name"] == "logo.png"


async def test_octet_stream_with_a_pdf_extension_is_refused(mcp, mail, prepare):
    mail["attachment"] = (b"%PDF-1.4 fake", "application/octet-stream", "invoice.pdf")

    with pytest.raises(
        MCPError,
        match=r"Attachment 'invoice\.pdf' is not an image \(application/pdf\)\. "
        r"Use nc_mail_get_attachment to fetch it\.",
    ):
        await mcp.call_tool("nc_mail_view_attachment", _ARGS)

    prepare.assert_not_called()


@pytest.mark.parametrize(
    ("mime", "name"),
    [
        ("application/pdf", "x.pdf"),
        ("text/plain", "notes.txt"),
        # A wrong name does not rescue a non-image type.
        ("application/pdf", "photo.jpg"),
    ],
)
async def test_attachments_that_are_not_images_are_refused_before_decoding(
    mcp, mail, prepare, mime, name
):
    mail["attachment"] = (_jpeg((100, 100)), mime, name)

    with pytest.raises(
        MCPError, match=rf"Attachment '{name}' is not an image \({mime}\)"
    ):
        await mcp.call_tool("nc_mail_view_attachment", _ARGS)

    prepare.assert_not_called()


async def test_undecodable_image_is_refused(mcp, mail):
    """E.g. HEIC: Pillow cannot read it, and Nextcloud has no mail preview."""
    mail["attachment"] = (b"ftypheic not decodable here", "image/heic", "IMG_1.heic")

    with pytest.raises(MCPError, match="Cannot show attachment 'IMG_1.heic'"):
        await mcp.call_tool("nc_mail_view_attachment", _ARGS)


# ── Download failures, as nc_mail_get_attachment reports them ───────────


async def test_missing_attachment_is_reported_as_not_found(mcp, mail, prepare):
    mail["attachment"] = _http_error(404)

    with pytest.raises(MCPError, match="^Attachment not found$"):
        await mcp.call_tool("nc_mail_view_attachment", _ARGS)

    prepare.assert_not_called()


async def test_other_http_errors_report_the_status(mcp, mail):
    mail["attachment"] = _http_error(500)

    with pytest.raises(MCPError, match="^Failed to get attachment: 500$"):
        await mcp.call_tool("nc_mail_view_attachment", _ARGS)


async def test_network_errors_are_reported(mcp, mail):
    mail["attachment"] = _request_error()

    with pytest.raises(
        MCPError, match="^Network error getting attachment: connection refused$"
    ):
        await mcp.call_tool("nc_mail_view_attachment", _ARGS)


# ── As the model sees it: NextcloudMCPServer, over a real client ────────


@pytest.fixture
def nextcloud_mcp() -> NextcloudMCPServer:
    server = NextcloudMCPServer("test-mail-view-attachment-client")
    configure_mail_tools(server)
    return server


async def test_failures_reach_the_model_as_error_results(nextcloud_mcp, mail):
    """Production maps MCPError to ToolError, so the model reads the message
    instead of the client raising a protocol error."""
    mail["attachment"] = (b"%PDF-1.4 fake", "application/pdf", "x.pdf")

    async with Client(nextcloud_mcp) as client:
        result = await client.call_tool("nc_mail_view_attachment", _ARGS)

    assert result.is_error is True
    assert (
        "Attachment 'x.pdf' is not an image (application/pdf). "
        "Use nc_mail_get_attachment to fetch it."
    ) in result.content[0].text


async def test_missing_attachment_reaches_the_model_as_an_error_result(
    nextcloud_mcp, mail
):
    mail["attachment"] = _http_error(404)

    async with Client(nextcloud_mcp) as client:
        result = await client.call_tool("nc_mail_view_attachment", _ARGS)

    assert result.is_error is True
    assert "Attachment not found" in result.content[0].text


async def test_image_reaches_the_client_as_an_image_block(nextcloud_mcp, mail):
    async with Client(nextcloud_mcp) as client:
        result = await client.call_tool("nc_mail_view_attachment", _ARGS)

    assert result.is_error is False
    image = result.content[0]
    assert isinstance(image, ImageContent)
    assert Image.open(io.BytesIO(base64.b64decode(image.data))).size == (1568, 1176)
    assert result.structured_content["name"] == "photo.jpg"


# ── Registration ────────────────────────────────────────────────────────


def test_registration(mcp):
    tool = {t.name: t for t in mcp._tool_manager.list_tools()}[
        "nc_mail_view_attachment"
    ]

    assert tool.title == "View Mail Attachment"
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is True
    assert list(tool.fn._required_scopes) == ["mail.read"]
    assert set(tool.output_schema["properties"]) >= {
        "message_id",
        "attachment_id",
        "name",
        "mime_type",
        "width",
        "height",
        "source_width",
        "source_height",
        "size",
    }


def test_the_description_says_what_agents_need_to_choose_it(mcp):
    description = {t.name: t for t in mcp._tool_manager.list_tools()}[
        "nc_mail_view_attachment"
    ].description

    for fact in (
        "instead of nc_mail_get_attachment",
        "max_size",
        "1568",
        "2576",
        "upright",
        "HEIC",
        "are not images",
        "id: null",
    ):
        assert fact in description, fact


def test_readers_point_to_the_view_tool(mcp):
    """Agents pick tools by description: the base64 path names the better one."""
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert "nc_mail_view_attachment" in tools["nc_mail_get_attachment"].description
    assert "nc_mail_view_attachment" in tools["nc_mail_get_message"].description
