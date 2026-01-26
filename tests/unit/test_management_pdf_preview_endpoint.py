"""
Unit tests for Management API PDF preview endpoint.

Tests the /api/v1/pdf-preview endpoint focusing on:
- Parameter validation (file_path, page, scale)
- OAuth token validation
- PDF rendering with PyMuPDF
- Error handling (file not found, invalid page, etc.)
"""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from nextcloud_mcp_server.api.visualization import get_pdf_preview

pytestmark = pytest.mark.unit


def create_test_app():
    """Create a test Starlette app with the PDF preview endpoint."""
    app = Starlette(
        routes=[
            Route("/api/v1/pdf-preview", get_pdf_preview, methods=["GET"]),
        ]
    )
    # Set up OAuth context (required by endpoint)
    app.state.oauth_context = {"config": {"nextcloud_host": "http://localhost:8080"}}
    return app


def create_mock_pdf_bytes():
    """Create a minimal valid PDF for testing."""
    # Minimal PDF structure that PyMuPDF can parse
    # This is a 1-page PDF with a blank page
    pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>
endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer
<< /Size 4 /Root 1 0 R >>
startxref
196
%%EOF"""
    return pdf_content


class TestPdfPreviewParameterValidation:
    """Tests for parameter validation in PDF preview endpoint."""

    def test_missing_file_path_returns_400(self):
        """Test that missing file_path parameter returns 400."""
        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
            assert "file_path" in data["error"].lower()

    def test_invalid_page_number_returns_400(self):
        """Test that invalid page number (0 or negative) returns 400."""
        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
        ):
            app = create_test_app()
            client = TestClient(app)

            # Test page=0
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&page=0",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
            assert "page" in data["error"].lower()

            # Test negative page
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&page=-1",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 400

    def test_invalid_scale_returns_400(self):
        """Test that scale outside valid range returns 400."""
        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
        ):
            app = create_test_app()
            client = TestClient(app)

            # Test scale too small
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&scale=0.1",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
            assert "scale" in data["error"].lower()

            # Test scale too large
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&scale=10.0",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 400

    def test_non_numeric_page_returns_400(self):
        """Test that non-numeric page parameter returns 400."""
        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&page=abc",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False


class TestPdfPreviewAuthentication:
    """Tests for authentication in PDF preview endpoint."""

    def test_unauthorized_without_token_returns_401(self):
        """Test that request without token returns 401."""
        with patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new_callable=AsyncMock,
            side_effect=Exception("Invalid token"),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get("/api/v1/pdf-preview?file_path=/test.pdf")

            assert response.status_code == 401
            data = response.json()
            assert data["success"] is False

    def test_unauthorized_with_invalid_token_returns_401(self):
        """Test that request with invalid token returns 401."""
        with patch(
            "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
            new_callable=AsyncMock,
            side_effect=Exception("Token expired"),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf",
                headers={"Authorization": "Bearer invalid-token"},
            )

            assert response.status_code == 401
            data = response.json()
            assert data["success"] is False


class TestPdfPreviewRendering:
    """Tests for PDF rendering functionality."""

    def test_successful_pdf_render(self):
        """Test successful PDF page rendering."""
        pdf_bytes = create_mock_pdf_bytes()

        # Mock the WebDAV client
        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(return_value=(pdf_bytes, "application/pdf"))

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&page=1&scale=1.0",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert "image" in data
            assert data["page_number"] == 1
            assert data["total_pages"] == 1

            # Verify image is valid base64
            try:
                decoded = base64.b64decode(data["image"])
                # PNG magic bytes
                assert decoded[:8] == b"\x89PNG\r\n\x1a\n"
            except Exception as e:
                pytest.fail(f"Image is not valid base64-encoded PNG: {e}")

    def test_page_out_of_range_returns_400(self):
        """Test that requesting page beyond total pages returns 400."""
        pdf_bytes = create_mock_pdf_bytes()

        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(return_value=(pdf_bytes, "application/pdf"))

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&page=999",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
            assert "page" in data["error"].lower()
            assert "999" in data["error"]

    def test_file_not_found_returns_404(self):
        """Test that non-existent file returns 404."""
        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(
            side_effect=FileNotFoundError("File not found")
        )

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/nonexistent.pdf",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 404
            data = response.json()
            assert data["success"] is False
            assert "not found" in data["error"].lower()

    def test_default_parameters(self):
        """Test that default parameters (page=1, scale=2.0) are used."""
        pdf_bytes = create_mock_pdf_bytes()

        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(return_value=(pdf_bytes, "application/pdf"))

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            # Only file_path, no page or scale
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["page_number"] == 1  # Default page


class TestPdfPreviewEdgeCases:
    """Tests for edge cases in PDF preview endpoint."""

    def test_url_encoded_file_path(self):
        """Test that URL-encoded file paths are handled correctly."""
        pdf_bytes = create_mock_pdf_bytes()

        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(return_value=(pdf_bytes, "application/pdf"))

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            # URL-encoded path with spaces
            response = client.get(
                "/api/v1/pdf-preview?file_path=/Documents/My%20File.pdf",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 200
            # Verify the path was passed correctly to WebDAV
            mock_webdav.read_file.assert_called_once()
            call_args = mock_webdav.read_file.call_args[0]
            assert "My File.pdf" in call_args[0]

    def test_missing_nextcloud_host_config(self):
        """Test handling when Nextcloud host is not configured."""
        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
        ):
            app = create_test_app()
            # Override with empty config
            app.state.oauth_context = {"config": {"nextcloud_host": ""}}

            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 500
            data = response.json()
            assert data["success"] is False

    def test_corrupted_pdf_returns_400(self):
        """Test that corrupted PDF data returns 400 with specific error."""
        mock_webdav = AsyncMock()
        # Return invalid PDF bytes
        mock_webdav.read_file = AsyncMock(
            return_value=(b"not a valid pdf", "application/pdf")
        )

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/corrupted.pdf",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
            assert (
                "corrupted" in data["error"].lower()
                or "invalid" in data["error"].lower()
            )

    def test_boundary_scale_values(self):
        """Test boundary scale values (min and max)."""
        pdf_bytes = create_mock_pdf_bytes()

        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(return_value=(pdf_bytes, "application/pdf"))

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)

            # Test minimum valid scale (0.5)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&scale=0.5",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 200

            # Test maximum valid scale (5.0)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/test.pdf&scale=5.0",
                headers={"Authorization": "Bearer test-token"},
            )
            assert response.status_code == 200


class TestPdfPreviewSecurityValidation:
    """Tests for security validations in PDF preview endpoint."""

    def test_path_traversal_returns_400(self):
        """Test that path traversal attempts are blocked with 400."""
        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
        ):
            app = create_test_app()
            client = TestClient(app)

            # Test various path traversal patterns
            traversal_paths = [
                "/Documents/../../../etc/passwd",
                "/../secret.pdf",
                "/folder/..%2F..%2Fetc/passwd",  # URL-encoded
                "/test/../secret.pdf",
            ]

            for path in traversal_paths:
                response = client.get(
                    f"/api/v1/pdf-preview?file_path={path}",
                    headers={"Authorization": "Bearer test-token"},
                )
                assert response.status_code == 400, (
                    f"Path traversal not blocked: {path}"
                )
                data = response.json()
                assert data["success"] is False
                assert "invalid file path" in data["error"].lower()

    def test_file_size_limit_exceeded_returns_413(self):
        """Test that files exceeding 50MB limit return 413."""
        # Create bytes larger than 50MB limit
        large_pdf_bytes = b"x" * (51 * 1024 * 1024)  # 51 MB

        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(
            return_value=(large_pdf_bytes, "application/pdf")
        )

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/large.pdf",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 413
            data = response.json()
            assert data["success"] is False
            assert "size limit" in data["error"].lower()

    def test_corrupted_pdf_returns_400(self):
        """Test that corrupted PDF returns 400 with specific error message."""
        # Invalid PDF content that PyMuPDF cannot parse
        corrupted_pdf_bytes = b"not a valid PDF file content"

        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(
            return_value=(corrupted_pdf_bytes, "application/pdf")
        )

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/corrupted.pdf",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
            assert (
                "corrupted" in data["error"].lower()
                or "invalid" in data["error"].lower()
            )

    def test_empty_pdf_returns_400(self):
        """Test that empty PDF file returns 400."""
        empty_pdf_bytes = b""

        mock_webdav = AsyncMock()
        mock_webdav.read_file = AsyncMock(
            return_value=(empty_pdf_bytes, "application/pdf")
        )

        mock_nc_client = MagicMock()
        mock_nc_client.webdav = mock_webdav
        mock_nc_client.__aenter__ = AsyncMock(return_value=mock_nc_client)
        mock_nc_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "nextcloud_mcp_server.api.visualization.validate_token_and_get_user",
                new_callable=AsyncMock,
                return_value=("testuser", True),
            ),
            patch(
                "nextcloud_mcp_server.api.visualization.extract_bearer_token",
                return_value="test-token",
            ),
            patch(
                "nextcloud_mcp_server.client.NextcloudClient.from_token",
                return_value=mock_nc_client,
            ),
        ):
            app = create_test_app()
            client = TestClient(app)
            response = client.get(
                "/api/v1/pdf-preview?file_path=/empty.pdf",
                headers={"Authorization": "Bearer test-token"},
            )

            assert response.status_code == 400
            data = response.json()
            assert data["success"] is False
