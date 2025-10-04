"""HTTP client for Unstructured API."""

import io
import logging
from typing import Optional, Tuple

import httpx

from nextcloud_mcp_server.config import get_unstructured_api_url

logger = logging.getLogger(__name__)


class UnstructuredClient:
    """Client for interacting with the Unstructured API.
    
    The Unstructured API provides document parsing capabilities for various formats
    including PDF, DOCX, images with OCR, and more.
    
    API Documentation: https://docs.unstructured.io/api-reference/api-services/api-parameters
    """
    
    def __init__(self, api_url: Optional[str] = None, timeout: int = 120):
        """Initialize the Unstructured API client.
        
        Args:
            api_url: Base URL of the Unstructured API. If None, will use config.
            timeout: Request timeout in seconds (default: 120 for large documents)
        """
        self.api_url = api_url or get_unstructured_api_url()
        self.timeout = timeout
        
        if not self.api_url:
            raise ValueError(
                "Unstructured API URL not configured. "
                "Set ENABLE_UNSTRUCTURED_PARSING=true and UNSTRUCTURED_API_URL in environment."
            )
        
        logger.info(f"Initialized UnstructuredClient with API URL: {self.api_url}")
    
    async def partition_document(
        self,
        content: bytes,
        filename: str,
        content_type: Optional[str] = None,
        strategy: str = "auto",
        languages: Optional[list[str]] = None,
        extract_image_block_types: Optional[list[str]] = None,
    ) -> Tuple[str, dict]:
        """Parse a document using the Unstructured API.
        
        Args:
            content: The document content as bytes
            filename: The filename (used for format detection)
            content_type: Optional MIME type
            strategy: Parsing strategy - "auto", "fast", or "hi_res" (OCR-based)
            languages: List of language codes for OCR (e.g., ["eng", "deu"])
            extract_image_block_types: Types of elements to extract from images
            
        Returns:
            Tuple of (parsed_text, metadata) where:
            - parsed_text: The extracted text content
            - metadata: Additional metadata about the parsing
            
        Raises:
            httpx.HTTPError: If the API request fails
            Exception: If parsing fails
        """
        if languages is None:
            languages = ["eng"]  # Default to English
        
        # Prepare the multipart form data
        files = {
            "files": (filename, io.BytesIO(content), content_type or "application/octet-stream")
        }
        
        # Prepare the request data
        data = {
            "strategy": strategy,
            "languages": ",".join(languages),
        }
        
        if extract_image_block_types:
            data["extract_image_block_types"] = ",".join(extract_image_block_types)
        
        logger.debug(
            f"Partitioning document '{filename}' with strategy '{strategy}', "
            f"languages: {languages}"
        )
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.api_url}/general/v0/general",
                    files=files,
                    data=data,
                )
                response.raise_for_status()
                
                # Parse the response
                elements = response.json()
                
                # Extract text from elements
                # Each element has a "text" field
                texts = []
                element_types = {}
                
                for element in elements:
                    if "text" in element and element["text"]:
                        texts.append(element["text"])
                    
                    # Track element types
                    el_type = element.get("type", "unknown")
                    element_types[el_type] = element_types.get(el_type, 0) + 1
                
                parsed_text = "\n\n".join(texts)
                
                # Collect metadata
                metadata = {
                    "element_count": len(elements),
                    "text_length": len(parsed_text),
                    "element_types": element_types,
                    "strategy": strategy,
                    "languages": languages,
                    "parsing_method": "unstructured_api"
                }
                
                logger.debug(
                    f"Successfully parsed document: {len(elements)} elements, "
                    f"{len(parsed_text)} characters"
                )
                
                return parsed_text, metadata
                
        except httpx.HTTPError as e:
            logger.error(f"HTTP error calling Unstructured API: {e}")
            raise Exception(f"Failed to parse document via Unstructured API: {str(e)}") from e
        except Exception as e:
            logger.error(f"Unexpected error parsing document: {e}")
            raise Exception(f"Failed to parse document: {str(e)}") from e
    
    async def health_check(self) -> bool:
        """Check if the Unstructured API is available.
        
        Returns:
            True if the API is healthy, False otherwise.
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.api_url}/healthcheck")
                return response.status_code == 200
        except Exception as e:
            logger.warning(f"Unstructured API health check failed: {e}")
            return False