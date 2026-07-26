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
# The VLM round-trip needs a VLM-capable docling-serve (a VLM preset backed by a
# real inference engine, e.g. glm_ocr via Ollama). The CI docling-serve-cpu image
# has no such engine, so the VLM test is opt-in: it runs only when the operator
# has deployed the MCP service with DOCLING_PIPELINE=vlm against such an instance.
_DOCLING_VLM = _DOCLING_ENABLED and os.getenv("DOCLING_PIPELINE", "").lower() == "vlm"
# Docling reaches PDFs only as the OCR provider (Deck #894 removed the per-call
# processor override), which needs the OCR tier switched on as well.
_DOCLING_OCR = (
    _DOCLING_ENABLED
    and os.getenv("DOCUMENT_OCR_ENABLED", "false").lower() == "true"
    and os.getenv("DOCUMENT_OCR_PROVIDER", "").lower() == "docling"
)


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


def create_scanned_pdf(text: str) -> bytes:
    """A PDF whose only content is a raster image -- no text layer at all.

    This is what makes the tier-0 classifier recommend OCR: a text extractor has
    nothing to extract, which is exactly the "scanned document" case.
    """
    from reportlab.lib.utils import ImageReader  # noqa: PLC0415

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.drawImage(
        ImageReader(BytesIO(create_text_image(text))),
        60,
        520,
        width=480,
        height=128,
    )
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


@pytest.mark.skipif(
    not _DOCLING_VLM,
    reason="Docling VLM pipeline is not configured (DOCLING_PIPELINE=vlm)",
)
async def test_docling_vlm_image_parsing(
    nc_client: NextcloudClient, test_base_path: str, nc_mcp_client: ClientSession
):
    """With DOCLING_PIPELINE=vlm the same image auto-routes to docling and the VLM
    preset transcribes it. parsing_method stays "docling"; the pipeline surfaces in
    parsing_metadata. Manual/opt-in: needs a VLM-capable docling-serve."""
    test_file = f"{test_base_path}/docling_vlm_image.png"
    marker = "DoclingVlmHello"
    try:
        await nc_client.webdav.write_file(
            test_file, create_text_image(marker), content_type="image/png"
        )
        mcp_result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": test_file}
        )
        result = _read_result(mcp_result)

        assert result.get("parsed") is True
        metadata = result["parsing_metadata"]
        assert metadata["parsing_method"] == "docling"
        assert metadata.get("docling_pipeline") == "vlm"
        content = result["content"]
        assert isinstance(content, str) and content
        assert "docling" in content.lower()
    finally:
        try:
            await nc_client.webdav.delete_resource(test_file)
        except Exception:
            pass


@pytest.mark.skipif(
    not _DOCLING_OCR,
    reason="Docling OCR provider is not configured (DOCUMENT_OCR_PROVIDER=docling)",
)
async def test_docling_ocr_tier_reads_a_scanned_pdf(
    nc_client: NextcloudClient, test_base_path: str, nc_mcp_client: ClientSession
):
    """A scanned PDF escalates fast -> ocr and docling returns its text.

    Docling is an OCR *provider*, not a tier of its own: since Deck #894 there is
    no per-call processor override, so this is the route by which a PDF reaches
    docling at all. The PDF has no text layer, so the tier-0 classifier is what
    triggers the escalation.
    """
    test_file = f"{test_base_path}/docling_scanned.pdf"
    marker = "DoclingScannedHello"
    try:
        await nc_client.webdav.write_file(
            test_file, create_scanned_pdf(marker), content_type="application/pdf"
        )
        mcp_result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": test_file}
        )
        result = _read_result(mcp_result)

        assert result.get("parsed") is True
        assert result["parse_tier"] == "ocr"
        # OCR output is markdown whatever the backend, and the read says so.
        assert result["content_format"] == "markdown"
        # OCR is imperfect; assert on a distinctive substring rather than equality.
        assert "docling" in result["content"].lower()
    finally:
        try:
            await nc_client.webdav.delete_resource(test_file)
        except Exception:
            pass
