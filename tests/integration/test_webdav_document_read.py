"""End-to-end coverage of reading a document through nc_webdav_read_file (Deck #894).

Runs against the single-user MCP service (port 8000) with the built-in PDF tiers
-- no optional processor, no OCR backend, nothing enabled beyond a default
deployment. That is the point: an agent reading a PDF gets its text out of the
box, and the response says which tier produced it.
"""

import json
import logging
import uuid
from io import BytesIO

import pytest
from mcp.client.session import ClientSession
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration

MARKER = "ParseDocumentIntegrationMarker"


@pytest.fixture
async def test_base_path(nc_client: NextcloudClient):
    test_dir = f"mcp_test_doc_read_{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(test_dir)
    yield test_dir
    try:
        await nc_client.webdav.delete_resource(test_dir)
    except Exception:
        pass  # Ignore cleanup errors


#: Ordinary prose, on purpose. The tier-0 classifier scores the extracted text
#: layer, and a page holding one short unspaced token reads as low-quality: it
#: escalates fast->structured (verified — a single ``drawString(MARKER)`` page
#: lands on the structured tier and comes back as markdown). That would make this
#: file a test of classifier heuristics rather than of the read path, and would
#: let the markdown test below pass without markdown ever being requested. Keep
#: the body realistic if you touch it.
_BODY = [
    "This document exists so an integration test can read a real PDF back",
    "through the MCP tool and check that its text comes out as text rather",
    "than as base64, which is what this tool used to return for any document.",
    "The wording is deliberately ordinary: several lines of normal words give",
    "the tier-0 classifier a text layer it can be confident about, so the read",
    "stops at the cheap fast tier instead of escalating to recover it.",
]


@pytest.fixture
async def text_layer_pdf(nc_client: NextcloudClient, test_base_path: str):
    """A born-digital single-page PDF with a healthy text layer."""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.drawString(72, 750, f"Quarterly Report {MARKER}")
    for offset, line in enumerate(_BODY, start=1):
        c.drawString(72, 750 - offset * 20, line)
    c.save()

    path = f"{test_base_path}/document.pdf"
    await nc_client.webdav.write_file(
        path, buffer.getvalue(), content_type="application/pdf"
    )
    return path


def _read_result(mcp_result) -> dict:
    content = mcp_result.content[0]
    text = content.text if hasattr(content, "text") else str(content)
    return json.loads(text)


async def _read(nc_mcp_client: ClientSession, path: str, **arguments) -> dict:
    return _read_result(
        await nc_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": path, **arguments}
        )
    )


async def test_pdf_reads_as_text_by_default(
    nc_mcp_client: ClientSession, text_layer_pdf: str
):
    """The default is text, not base64 -- the whole point of the card."""
    result = await _read(nc_mcp_client, text_layer_pdf)

    assert result["parsed"] is True
    assert result["parse_status"] == "parsed"
    assert result["parse_tier"] == "fast"
    assert result["content_format"] == "text"
    assert result.get("encoding") is None
    assert MARKER in result["content"]
    # A clean parse has nothing to disclose.
    assert result["parse_notes"] == []
    # The etag survives the streamed download, so the caller can still write back
    # with an If-Match precondition.
    assert result["etag"]


async def test_markdown_mode_reconstructs_structure(
    nc_mcp_client: ClientSession, text_layer_pdf: str
):
    """Asking for markdown promotes the read to the structured tier."""
    result = await _read(nc_mcp_client, text_layer_pdf, parse_document="markdown")

    assert result["parse_status"] == "parsed"
    assert result["parse_tier"] == "structured"
    assert result["content_format"] == "markdown"
    assert MARKER in result["content"]


async def test_raw_mode_returns_the_bytes(
    nc_mcp_client: ClientSession, text_layer_pdf: str
):
    """The raw path stays reachable for a caller that wants the file itself."""
    result = await _read(nc_mcp_client, text_layer_pdf, parse_document="raw")

    assert result["parsed"] is False
    assert result["parse_status"] == "skipped"
    assert result["encoding"] == "base64"
    assert result["content_format"] == "base64"
    assert result["parse_notes"] == []


async def test_text_file_is_untouched_by_the_parse_argument(
    nc_client: NextcloudClient, nc_mcp_client: ClientSession, test_base_path: str
):
    """A plain text file needs no processor and is reported as such."""
    path = f"{test_base_path}/notes.txt"
    await nc_client.webdav.write_file(path, b"hello world", content_type="text/plain")

    result = await _read(nc_mcp_client, path)

    assert result["content"] == "hello world"
    assert result["parse_status"] == "not_applicable"
    assert result["parsed"] is False
    assert result["parse_notes"] == []
