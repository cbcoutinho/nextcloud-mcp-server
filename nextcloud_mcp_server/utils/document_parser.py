"""Document parsing utilities using pluggable processor registry."""

import base64
import logging
from typing import Optional, Tuple

from nextcloud_mcp_server.config import get_document_processor_config
from nextcloud_mcp_server.document_processors import (
    ProcessorError,
    get_registry,
)

logger = logging.getLogger(__name__)


def is_parseable_document(content_type: Optional[str]) -> bool:
    """Check if a document type can be parsed by any registered processor.

    Args:
        content_type: The MIME type of the document

    Returns:
        True if any processor can handle this type, False otherwise
    """
    if not content_type:
        return False

    config = get_document_processor_config()
    if not config["enabled"]:
        return False

    registry = get_registry()
    processor = registry.find_processor(content_type)
    return processor is not None


async def parse_document(
    content: bytes, content_type: Optional[str], filename: Optional[str] = None
) -> Tuple[str, dict]:
    """Parse a document using registered processors.

    This function uses the processor registry to find an appropriate
    processor for the given document type and extract text from it.

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
    if not content_type:
        raise ValueError("Content type is required for document parsing")

    config = get_document_processor_config()
    if not config["enabled"]:
        raise ValueError("Document processing is disabled")

    registry = get_registry()

    logger.debug(f"Parsing document of type '{content_type}'")

    try:
        # Process using registry (auto-selects processor based on MIME type)
        result = await registry.process(
            content=content,
            content_type=content_type,
            filename=filename,
        )

        logger.info(f"Successfully parsed document with '{result.processor}' processor")

        return result.text, result.metadata

    except ProcessorError as e:
        logger.error(f"Document processing failed: {e}")
        # Fallback to base64 with error metadata
        parsed_text = f"Document could not be parsed. Base64 content: {base64.b64encode(content).decode('ascii')[:200]}..."
        metadata = {
            "mime_type": content_type,
            "text_length": len(parsed_text),
            "parsing_method": "fallback_base64",
            "error": str(e),
        }
        return parsed_text, metadata
