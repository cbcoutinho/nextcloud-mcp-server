"""End-to-end tests for ``nc_webdav_view_image`` against a live Nextcloud.

Each test uploads its own image directly over WebDAV, calls the tool through
the MCP client, and decodes the returned image block, so the assertions are
about what a vision model would actually receive.
"""

import base64
import io
import uuid

import pytest
from PIL import Image

pytestmark = [pytest.mark.integration]


def _encode(image: Image.Image, fmt: str, **kwargs) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


@pytest.fixture
async def uploaded(nc_client):
    """Upload bytes as ``mcp_image_<uuid><suffix>``; delete them afterwards."""
    created: list[str] = []

    async def _upload(body: bytes, suffix: str, content_type: str) -> str:
        path = f"mcp_image_{uuid.uuid4().hex[:8]}{suffix}"
        await nc_client.webdav.write_file(path, body, content_type)
        created.append(path)
        return path

    yield _upload

    for path in created:
        try:
            await nc_client.webdav.delete_resource(path)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def _image_block(result):
    assert result.is_error is False, f"tool failed: {result.content}"
    block = result.content[0]
    assert block.type == "image", f"first block is {block.type}, not an image"
    return block, Image.open(io.BytesIO(base64.b64decode(block.data)))


async def test_large_photo_arrives_as_a_scaled_image(nc_mcp_client, uploaded):
    """Made from Nextcloud's preview, scaled to the default 1568 px."""
    path = await uploaded(
        _encode(Image.new("RGB", (4000, 3000), (20, 100, 180)), "JPEG"),
        ".jpg",
        "image/jpeg",
    )

    result = await nc_mcp_client.call_tool("nc_webdav_view_image", {"path": path})

    block, image = _image_block(result)
    assert block.mime_type == "image/jpeg"
    assert image.size == (1568, 1176)
    data = result.structured_content
    assert data["source"] == "preview"
    assert (data["source_width"], data["source_height"]) == (4000, 3000)


async def test_rotated_phone_photo_arrives_upright(nc_mcp_client, uploaded):
    """Phones store portraits sideways with EXIF orientation 6 (90° CW)."""
    image = Image.new("RGB", (4000, 3000), "gray")
    exif = image.getexif()
    exif[0x0112] = 6
    path = await uploaded(_encode(image, "JPEG", exif=exif), ".jpg", "image/jpeg")

    result = await nc_mcp_client.call_tool("nc_webdav_view_image", {"path": path})

    _, received = _image_block(result)
    assert received.size == (1176, 1568)


async def test_transparent_png_stays_png(nc_mcp_client, uploaded):
    """Nextcloud previews it as PNG, and the alpha channel survives."""
    path = await uploaded(
        _encode(Image.new("RGBA", (800, 400), (255, 0, 0, 0)), "PNG"),
        ".png",
        "image/png",
    )

    result = await nc_mcp_client.call_tool("nc_webdav_view_image", {"path": path})

    block, image = _image_block(result)
    assert block.mime_type == "image/png"
    assert image.mode == "RGBA"


async def test_max_size_bounds_the_image(nc_mcp_client, uploaded):
    """A small max_size asks Nextcloud for the 256 px preview step."""
    path = await uploaded(
        _encode(Image.new("RGB", (3000, 2000), "white"), "JPEG"), ".jpg", "image/jpeg"
    )

    result = await nc_mcp_client.call_tool(
        "nc_webdav_view_image", {"path": path, "max_size": 256}
    )

    _, image = _image_block(result)
    assert max(image.size) == 256


async def test_text_file_is_refused(nc_mcp_client, uploaded):
    """Nextcloud would render it as a picture of text, which is not an image."""
    path = await uploaded(b"just text\n", ".txt", "text/plain")

    result = await nc_mcp_client.call_tool("nc_webdav_view_image", {"path": path})

    assert result.is_error is True
    assert "not an image" in result.content[0].text
