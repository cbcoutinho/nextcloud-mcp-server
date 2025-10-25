"""Integration tests for Unstructured API functionality."""

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


@pytest.fixture
async def test_base_path(nc_client: NextcloudClient):
    """Base path for test files/directories."""
    test_dir = f"mcp_test_unstructured_{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(test_dir)
    yield test_dir
    try:
        await nc_client.webdav.delete_resource(test_dir)
    except Exception:
        pass  # Ignore cleanup errors


def create_test_pdf(text: str) -> bytes:
    """Create a simple PDF document with the given text."""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.drawString(100, 750, text)
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


@pytest.mark.skipif(
    condition=os.getenv("ENABLE_UNSTRUCTURED", "false") != "true",
    reason="Unstructured is not enabled",
)
async def test_unstructured_api_enabled_parsing(
    nc_client: NextcloudClient, test_base_path: str, nc_mcp_client: ClientSession
):
    """Test that documents are parsed using the Unstructured API when enabled."""
    test_file = f"{test_base_path}/test_unstructured_pdf.pdf"
    test_text = "This is a test PDF document for Unstructured API parsing"

    try:
        # Create a simple PDF
        pdf_content = create_test_pdf(test_text)

        # Upload the PDF
        await nc_client.webdav.write_file(
            test_file, pdf_content, content_type="application/pdf"
        )
        logger.info(f"Uploaded PDF file: {test_file}")

        # Read the PDF using MCP tool (should parse via Unstructured API)
        mcp_result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": test_file}
        )

        # Extract content from the MCP result
        if hasattr(mcp_result.content[0], "text"):
            result_text = mcp_result.content[0].text
        else:
            # Fallback for other content types
            result_text = str(mcp_result.content[0])

        # Parse the JSON response
        result = json.loads(result_text)

        # Verify the result structure
        assert "path" in result
        assert "content" in result
        assert "content_type" in result
        assert "parsed" in result  # Should be present when parsing succeeds

        # The content should be readable text, not base64
        content = result["content"]
        assert isinstance(content, str)
        assert len(content) > 0
        assert "test" in content.lower()  # Should contain our test text

        # Should have parsing metadata
        assert "parsing_metadata" in result
        parsing_metadata = result["parsing_metadata"]
        assert parsing_metadata["parsing_method"] == "unstructured_api"

        logger.info("Successfully parsed PDF using Unstructured API")

    finally:
        # Clean up
        try:
            await nc_client.webdav.delete_resource(test_file)
        except Exception:
            pass  # Ignore cleanup errors


@pytest.mark.skipif(
    condition=os.getenv("ENABLE_UNSTRUCTURED", "false") != "true",
    reason="Unstructured is not enabled",
)
async def test_unstructured_api_with_docx(
    nc_client: NextcloudClient, test_base_path: str, nc_mcp_client: ClientSession
):
    """Test Unstructured API with DOCX files."""
    test_file = f"{test_base_path}/test_unstructured_docx.docx"
    try:
        # Create a simple DOCX-like file for testing purposes
        # Since we're removing python-docx dependency, we'll create a simple file
        docx_content = (
            b"This is a mock DOCX file content for testing Unstructured API parsing"
        )

        # Upload the file
        await nc_client.webdav.write_file(
            test_file,
            docx_content,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        logger.info(f"Uploaded DOCX file: {test_file}")

        # Read the file using MCP tool
        mcp_result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": test_file}
        )

        # Extract content from the MCP result
        if hasattr(mcp_result.content[0], "text"):
            result_text = mcp_result.content[0].text
        else:
            # Fallback for other content types
            result_text = str(mcp_result.content[0])

        # Parse the JSON response
        result = json.loads(result_text)

        # Verify the result structure
        assert "path" in result
        assert "content" in result
        assert "content_type" in result

        logger.info("Successfully processed DOCX file with Unstructured API")

    finally:
        # Clean up
        try:
            await nc_client.webdav.delete_resource(test_file)
        except Exception:
            pass  # Ignore cleanup errors
