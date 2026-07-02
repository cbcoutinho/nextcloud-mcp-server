"""Integration tests for the docling-serve document-parsing backend.

Gated on ``ENABLE_DOCLING=true`` (and a reachable ``DOCLING_API_URL``). Run the
docling-serve compose profile first:

    docker compose --profile docling up -d docling

The first run downloads OCR models and CPU inference is slow, so these tests
allow a generous time budget.
"""

import json
import logging
import os
import uuid
from io import BytesIO

import pytest
from mcp.client.session import ClientSession
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# These tests drive the single-user MCP service (port 8000) against a live
# docling-serve instance started via the docker-compose "docling" profile. The
# CI "docling" lane selects them with ``-m docling``; the skipif keeps them out
# of a local full-suite run where docling isn't configured.
pytestmark = [pytest.mark.integration, pytest.mark.docling]

_DOCLING_ENABLED = os.getenv("ENABLE_DOCLING", "false").lower() == "true"


@pytest.fixture
async def test_base_path(nc_client: NextcloudClient):
    test_dir = f"mcp_test_docling_{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(test_dir)
    yield test_dir
    try:
        await nc_client.webdav.delete_resource(test_dir)
    except Exception:
        pass  # Ignore cleanup errors


def _read_result(mcp_result) -> dict:
    content = mcp_result.content[0]
    text = content.text if hasattr(content, "text") else str(content)
    return json.loads(text)


def create_text_image(text: str) -> bytes:
    """A white PNG with large black text -- legible to docling's OCR engine."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 240), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 56)
    except Exception:
        font = ImageFont.load_default()
    draw.text((30, 80), text, fill="black", font=font)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def create_text_pdf(text: str) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.drawString(100, 750, text)
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


@pytest.mark.skipif(not _DOCLING_ENABLED, reason="Docling is not enabled")
async def test_docling_image_parsing(
    nc_client: NextcloudClient, test_base_path: str, nc_mcp_client: ClientSession
):
    """An image auto-routes to docling (priority over unstructured) and its text is
    OCR'd back out through nc_webdav_read_file."""
    test_file = f"{test_base_path}/docling_image.png"
    marker = "DoclingOcrHello"
    try:
        await nc_client.webdav.write_file(
            test_file, create_text_image(marker), content_type="image/png"
        )
        mcp_result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": test_file}
        )
        result = _read_result(mcp_result)

        assert result.get("parsed") is True
        assert result["parsing_metadata"]["parsing_method"] == "docling"
        content = result["content"]
        assert isinstance(content, str) and content
        # OCR is imperfect; assert on a distinctive substring rather than equality.
        assert "docling" in content.lower()
    finally:
        try:
            await nc_client.webdav.delete_resource(test_file)
        except Exception:
            pass


@pytest.mark.skipif(not _DOCLING_ENABLED, reason="Docling is not enabled")
async def test_docling_force_on_text_layer_pdf(
    nc_client: NextcloudClient, test_base_path: str, nc_mcp_client: ClientSession
):
    """force_processor="docling" re-parses a PDF that already has a text layer
    (the override for tables / incomplete text)."""
    test_file = f"{test_base_path}/docling_force.pdf"
    test_text = "This text-layer PDF is force-parsed with docling"
    try:
        await nc_client.webdav.write_file(
            test_file, create_text_pdf(test_text), content_type="application/pdf"
        )
        mcp_result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file",
            arguments={"path": test_file, "force_processor": "docling"},
        )
        result = _read_result(mcp_result)

        assert result.get("parsed") is True
        assert result["parsing_metadata"]["parsing_method"] == "docling"
        assert "docling" in result["content"].lower()
    finally:
        try:
            await nc_client.webdav.delete_resource(test_file)
        except Exception:
            pass


@pytest.mark.skipif(not _DOCLING_ENABLED, reason="Docling is not enabled")
async def test_docling_unknown_force_processor_errors(
    nc_client: NextcloudClient, test_base_path: str, nc_mcp_client: ClientSession
):
    """An unknown forced processor name is a clear tool error, not a base64 dump."""
    test_file = f"{test_base_path}/docling_bad_force.pdf"
    try:
        await nc_client.webdav.write_file(
            test_file, create_text_pdf("hello"), content_type="application/pdf"
        )
        # The MCP client surfaces a tool failure as an ``isError`` result, not a
        # raised exception (matches the repo convention, e.g. _search_helpers.py),
        # so assert on the result rather than pytest.raises.
        mcp_result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file",
            arguments={"path": test_file, "force_processor": "does-not-exist"},
        )
        assert mcp_result.isError
        content = mcp_result.content[0]
        error_text = str(getattr(content, "text", content))
        assert "does-not-exist" in error_text
    finally:
        try:
            await nc_client.webdav.delete_resource(test_file)
        except Exception:
            pass
