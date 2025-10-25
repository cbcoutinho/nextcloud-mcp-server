"""Generic HTTP API processor wrapper for custom document processing services."""

import logging
from typing import Any, Optional

import httpx

from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)


class CustomHTTPProcessor(DocumentProcessor):
    """Generic HTTP API processor wrapper.

    Allows integration with any custom document processing API that follows
    a simple request/response pattern. This makes it easy to integrate your
    own text extraction services without writing a full processor.

    Expected API Contract:
        - POST request with file as multipart/form-data
        - Response: {"text": "extracted text", "metadata": {...}}

    Example:
        processor = CustomHTTPProcessor(
            name="my_ocr",
            api_url="https://my-ocr-service.com/process",
            api_key="secret",
            supported_types={"application/pdf", "image/jpeg"},
        )
        result = await processor.process(pdf_bytes, "application/pdf")
    """

    def __init__(
        self,
        api_url: str,
        api_key: Optional[str] = None,
        timeout: int = 60,
        supported_types: Optional[set[str]] = None,
        name: str = "custom",
    ):
        """Initialize custom HTTP processor.

        Args:
            api_url: Your API endpoint (should accept POST with multipart/form-data)
            api_key: Optional API key for authentication (sent as Bearer token)
            timeout: Request timeout in seconds (default: 60)
            supported_types: MIME types your API supports
            name: Unique name for this processor (default: "custom")
        """
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout
        self._name = name
        self._supported_types = supported_types or set()

        logger.info(f"Initialized CustomHTTPProcessor: {name} -> {api_url}")

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_mime_types(self) -> set[str]:
        return self._supported_types

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> ProcessingResult:
        """Process via custom HTTP API.

        Args:
            content: Document bytes
            content_type: MIME type
            filename: Optional filename
            options: Custom options (passed as form data to API)

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            ProcessorError: If API call fails
        """
        options = options or {}

        # Prepare request
        files = {"file": (filename or "document", content, content_type)}
        headers = {}

        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.api_url,
                    files=files,
                    headers=headers,
                    data=options,  # Pass options as form data
                )
                response.raise_for_status()

                # Parse response
                result = response.json()
                text = result.get("text", "")
                metadata = result.get("metadata", {})

                logger.debug(
                    f"Custom processor '{self.name}' extracted {len(text)} characters"
                )

                return ProcessingResult(
                    text=text,
                    metadata=metadata,
                    processor=self.name,
                    success=True,
                )

        except httpx.HTTPError as e:
            logger.error(f"Custom processor '{self.name}' HTTP error: {e}")
            raise ProcessorError(f"API call failed: {str(e)}") from e
        except Exception as e:
            logger.error(f"Custom processor '{self.name}' failed: {e}")
            raise ProcessorError(f"Processing failed: {str(e)}") from e

    async def health_check(self) -> bool:
        """Check if custom API is available.

        Returns:
            True if API responds with status < 500
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # Try GET request to check availability
                response = await client.get(
                    self.api_url,
                    headers={"User-Agent": "nextcloud-mcp-server"},
                )
                return response.status_code < 500
        except Exception as e:
            logger.warning(f"Custom processor '{self.name}' health check failed: {e}")
            return False
