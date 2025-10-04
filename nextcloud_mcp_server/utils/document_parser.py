"""Document parsing utilities based on the "unstructured" microservice"""

import logging
from typing import Optional, Tuple

from nextcloud_mcp_server.config import is_unstructured_parsing_enabled

logger = logging.getLogger(__name__)

# Mapping of MIME types to their corresponding parsing strategies
PARSEABLE_MIME_TYPES = {
    # PDF documents
    "application/pdf": "pdf",
    # Microsoft Word documents
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    # Microsoft PowerPoint
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    # Microsoft Excel
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    # Other document formats
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/epub+zip": "epub",
    # Email formats
    "message/rfc822": "eml",
    "application/vnd.ms-outlook": "msg",
    # Image formats (for OCR)
    "image/jpeg": "image",
    "image/png": "image",
    "image/tiff": "image",
    "image/bmp": "image",
}

def is_parseable_document(content_type: Optional[str]) -> bool:
    """Check if a document type can be parsed.
    
    Args:
        content_type: The MIME type of the document
         
    Returns:
        True if the document can be parsed, False otherwise
    """
    if not content_type:
        return False
    
    # Handle content types with additional parameters (e.g., "application/pdf; charset=utf-8")
    base_content_type = content_type.split(";")[0].strip().lower()
    return base_content_type in PARSEABLE_MIME_TYPES

async def parse_document(
    content: bytes,
    content_type: Optional[str],
    filename: Optional[str] = None
) -> Tuple[str, dict]:
    """Parse a document using the Unstructured API.
    
    Args:
        content: The document content as bytes
        content_type: The MIME type of the document
        filename: Optional filename to help with format detection
         
    Returns:
        Tuple of (parsed_text, metadata) where:
        - parsed_text: The extracted text content
        - metadata: Additional metadata about the parsing
         
    Raises:
        ValueError: If the document type is not supported
        Exception: If parsing fails
    """
    if not is_parseable_document(content_type):
        raise ValueError(f"Document type '{content_type}' is not supported for parsing")
    
    base_content_type = content_type.split(";")[0].strip().lower() if content_type else ""
    doc_type = PARSEABLE_MIME_TYPES.get(base_content_type, "unknown")
    
    logger.debug(f"Parsing document of type '{doc_type}' (MIME: {content_type})")
    
    # Check if unstructured parsing is enabled via environment
    if is_unstructured_parsing_enabled():
        logger.debug("Using Unstructured API for parsing")
        try:
            from nextcloud_mcp_server.client.unstructured_client import UnstructuredClient
            client = UnstructuredClient()
            # The client will automatically use environment configuration
            # (UNSTRUCTURED_STRATEGY and UNSTRUCTURED_LANGUAGES)
            return await client.partition_document(
                content=content,
                filename=filename or f"document.{doc_type}",
                content_type=content_type,
            )
        except Exception as e:
            logger.error(f"Unstructured API parsing failed: {e}")
            # If unstructured parsing fails, return base64 as fallback
            import base64
            parsed_text = f"Document could not be parsed. Base64 content: {base64.b64encode(content).decode('ascii')[:200]}..."
            metadata = {
                "document_type": doc_type,
                "mime_type": content_type,
                "element_count": 0,
                "text_length": len(parsed_text),
                "parsing_method": "fallback_base64",
                "error": str(e)
            }
            return parsed_text, metadata
    else:
        logger.debug("Unstructured parsing is disabled, returning base64 encoded content as fallback")
        import base64
        parsed_text = f"Document could not be parsed. Base64 content: {base64.b64encode(content).decode('ascii')[:200]}..."
        metadata = {
            "document_type": doc_type,
            "mime_type": content_type,
            "element_count": 0,
            "text_length": len(parsed_text),
            "parsing_method": "fallback_base64"
        }
        return parsed_text, metadata