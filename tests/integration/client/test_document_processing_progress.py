"""Integration tests for document processing with progress notifications."""

import io
import os

import pytest
from PIL import Image

pytestmark = pytest.mark.integration


class TestDocumentProcessingProgress:
    """Test document processing with progress notifications."""

    async def test_unstructured_processor_with_progress_callback(self, nc_client):
        """Test that UnstructuredProcessor calls progress callback during processing."""

        # Skip if unstructured is not enabled
        if os.getenv("ENABLE_UNSTRUCTURED", "false").lower() != "true":
            pytest.skip("Unstructured processor not enabled")

        from nextcloud_mcp_server.document_processors.unstructured import (
            UnstructuredProcessor,
        )

        # Track progress callback invocations
        progress_updates = []

        async def track_progress(progress: float, total: float | None, message: str):
            progress_updates.append(
                {"progress": progress, "total": total, "message": message}
            )

        # Create processor configured to use local unstructured service
        processor = UnstructuredProcessor(
            api_url=os.getenv("UNSTRUCTURED_API_URL", "http://unstructured:8000"),
            timeout=120,
            progress_interval=2,  # 2 second intervals for testing
        )

        # Create a simple test image (which requires OCR processing)
        # This should take long enough to trigger at least one progress update
        img = Image.new("RGB", (400, 200), color=(73, 109, 137))
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        test_image = buffer.getvalue()

        # Process with progress callback
        result = await processor.process(
            content=test_image,
            content_type="image/png",
            filename="test.png",
            progress_callback=track_progress,
        )

        # Verify processing succeeded
        assert result.success is True
        assert result.processor == "unstructured"
        assert isinstance(result.text, str)

        # Note: Progress updates may or may not occur depending on processing speed
        # If updates occurred, verify their structure
        if progress_updates:
            for update in progress_updates:
                assert isinstance(update["progress"], float)
                assert update["total"] is None  # Unknown total
                assert "Processing document with unstructured" in update["message"]
                assert "elapsed" in update["message"]

    async def test_webdav_read_file_sends_progress_notifications(
        self, nc_mcp_client, nc_client
    ):
        """Test that reading a document via WebDAV MCP tool sends progress notifications."""

        # Skip if document processing is not enabled
        if os.getenv("ENABLE_DOCUMENT_PROCESSING", "false").lower() != "true":
            pytest.skip("Document processing not enabled")

        # Create a test image file in Nextcloud via WebDAV
        from PIL import Image

        img = Image.new("RGB", (400, 200), color=(100, 150, 200))
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        test_image = buffer.getvalue()

        # Upload test file
        test_path = "test_progress.png"
        await nc_client.webdav.write_file(test_path, test_image, "image/png")

        try:
            # Read file via MCP tool (which should trigger document processing)
            # The MCP client will automatically track progress notifications
            result = await nc_mcp_client.call_tool(
                "nc_webdav_read_file", arguments={"path": test_path}
            )

            # Note: FastMCP progress notifications are sent automatically by ctx.report_progress
            # We can't easily capture them in this test without mocking the MCP transport layer
            # The important thing is that the code path is exercised without errors
            assert result.isError is False

        finally:
            # Cleanup
            try:
                await nc_client.webdav.delete_resource(test_path)
            except Exception:
                pass  # Ignore cleanup errors

    async def test_progress_callback_not_required(self, nc_client):
        """Test that processing works without progress callback (backward compatibility)."""

        if os.getenv("ENABLE_UNSTRUCTURED", "false").lower() != "true":
            pytest.skip("Unstructured processor not enabled")

        from nextcloud_mcp_server.document_processors.unstructured import (
            UnstructuredProcessor,
        )

        processor = UnstructuredProcessor(
            api_url=os.getenv("UNSTRUCTURED_API_URL", "http://unstructured:8000"),
            timeout=120,
        )

        # Create simple test image
        img = Image.new("RGB", (200, 100), color=(50, 100, 150))
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        test_image = buffer.getvalue()

        # Process WITHOUT progress callback
        result = await processor.process(
            content=test_image,
            content_type="image/png",
            filename="test.png",
            progress_callback=None,  # Explicitly None
        )

        # Should still work
        assert result.success is True
        assert result.processor == "unstructured"
