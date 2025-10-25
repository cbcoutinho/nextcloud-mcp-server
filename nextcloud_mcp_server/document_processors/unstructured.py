"""Document processor using Unstructured.io API."""

import io
import logging
from typing import Any, Optional

import httpx

from .base import DocumentProcessor, ProcessingResult, ProcessorError

logger = logging.getLogger(__name__)


class UnstructuredProcessor(DocumentProcessor):
    """Document processor using Unstructured.io API.

    The Unstructured API provides document parsing capabilities for various formats
    including PDF, DOCX, images with OCR, and more.

    API Documentation: https://docs.unstructured.io/api-reference/api-services/api-parameters
    """

    # Supported MIME types for Unstructured
    SUPPORTED_TYPES = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/rtf",
        "text/rtf",
        "application/vnd.oasis.opendocument.text",
        "application/epub+zip",
        "message/rfc822",
        "application/vnd.ms-outlook",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/bmp",
    }

    def __init__(
        self,
        api_url: str,
        timeout: int = 120,
        default_strategy: str = "auto",
        default_languages: Optional[list[str]] = None,
    ):
        """Initialize Unstructured processor.

        Args:
            api_url: Unstructured API endpoint
            timeout: Request timeout in seconds (default: 120)
            default_strategy: Default parsing strategy - "auto", "fast", or "hi_res"
            default_languages: Default OCR language codes (e.g., ["eng", "deu"])
        """
        self.api_url = api_url
        self.timeout = timeout
        self.default_strategy = default_strategy
        self.default_languages = default_languages or ["eng"]

        logger.info(
            f"Initialized UnstructuredProcessor: {api_url}, "
            f"strategy={default_strategy}, languages={self.default_languages}"
        )

    @property
    def name(self) -> str:
        return "unstructured"

    @property
    def supported_mime_types(self) -> set[str]:
        return self.SUPPORTED_TYPES

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> ProcessingResult:
        """Process document via Unstructured API.

        Args:
            content: Document bytes
            content_type: MIME type
            filename: Optional filename for format detection
            options: Processing options:
                - strategy: "auto", "fast", or "hi_res" (default: from init)
                - languages: List of language codes (default: from init)
                - extract_image_block_types: Types of image elements to extract

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            ProcessorError: If processing fails
        """
        options = options or {}

        # Extract options with defaults
        strategy = options.get("strategy", self.default_strategy)
        languages = options.get("languages", self.default_languages)
        extract_image_block_types = options.get("extract_image_block_types")

        # Prepare multipart request
        files = {
            "files": (
                filename or "document",
                io.BytesIO(content),
                content_type or "application/octet-stream",
            )
        }

        data = {
            "strategy": strategy,
            "languages": ",".join(languages),
        }

        if extract_image_block_types:
            data["extract_image_block_types"] = ",".join(extract_image_block_types)

        logger.debug(
            f"Processing with Unstructured API: strategy={strategy}, languages={languages}"
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.api_url}/general/v0/general",
                    files=files,
                    data=data,
                )
                response.raise_for_status()

                # Parse response
                elements = response.json()

                # Extract text and metadata
                texts = []
                element_types: dict[str, int] = {}

                for element in elements:
                    if "text" in element and element["text"]:
                        texts.append(element["text"])

                    el_type = element.get("type", "unknown")
                    element_types[el_type] = element_types.get(el_type, 0) + 1

                parsed_text = "\n\n".join(texts)

                metadata = {
                    "element_count": len(elements),
                    "text_length": len(parsed_text),
                    "element_types": element_types,
                    "strategy": strategy,
                    "languages": languages,
                }

                logger.debug(
                    f"Successfully processed: {len(elements)} elements, "
                    f"{len(parsed_text)} characters"
                )

                return ProcessingResult(
                    text=parsed_text,
                    metadata=metadata,
                    processor=self.name,
                    success=True,
                )

        except httpx.HTTPError as e:
            logger.error(f"Unstructured API HTTP error: {e}")
            raise ProcessorError(f"HTTP error: {str(e)}") from e
        except Exception as e:
            logger.error(f"Unstructured API processing failed: {e}")
            raise ProcessorError(f"Processing failed: {str(e)}") from e

    async def health_check(self) -> bool:
        """Check if Unstructured API is available.

        Returns:
            True if API is healthy, False otherwise
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.api_url}/healthcheck")
                return response.status_code == 200
        except Exception as e:
            logger.warning(f"Unstructured health check failed: {e}")
            return False
