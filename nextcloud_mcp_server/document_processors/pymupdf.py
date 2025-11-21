"""Document processor using PyMuPDF (fitz) library."""

import logging
import pathlib
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import pymupdf
import pymupdf.layout

from .base import DocumentProcessor, ProcessingResult, ProcessorError

# Activate layout analysis for better text extraction
pymupdf.layout.activate()
import pymupdf4llm  # noqa

logger = logging.getLogger(__name__)


class PyMuPDFProcessor(DocumentProcessor):
    """Document processor using PyMuPDF library for PDF processing.

    PyMuPDF (fitz) is a fast, local PDF processing library that extracts text,
    metadata, and images without requiring external API calls.

    Features:
    - Fast text extraction with layout preservation
    - PDF metadata extraction (title, author, creation date, page count)
    - Image extraction for future multimodal support
    - Page number tracking for precise citations
    """

    SUPPORTED_TYPES = {
        "application/pdf",
    }

    def __init__(
        self,
        extract_images: bool = True,
        image_dir: Optional[str | pathlib.Path] = None,
    ):
        """Initialize PyMuPDF processor.

        Args:
            extract_images: Whether to extract embedded images from PDFs
            image_dir: Directory to store extracted images (defaults to temp directory)
        """
        self.extract_images = extract_images

        if image_dir is None:
            self.image_dir = pathlib.Path(tempfile.gettempdir()) / "pdf-images"
        else:
            self.image_dir = pathlib.Path(image_dir)

        # Create image directory if it doesn't exist
        if self.extract_images:
            self.image_dir.mkdir(exist_ok=True, parents=True)
            logger.info(
                f"Initialized PyMuPDFProcessor with image extraction to {self.image_dir}"
            )
        else:
            logger.info("Initialized PyMuPDFProcessor without image extraction")

    @property
    def name(self) -> str:
        return "pymupdf"

    @property
    def supported_mime_types(self) -> set[str]:
        return self.SUPPORTED_TYPES

    async def process(
        self,
        content: bytes,
        content_type: str,
        filename: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
        progress_callback: Optional[
            Callable[[float, Optional[float], Optional[str]], Awaitable[None]]
        ] = None,
    ) -> ProcessingResult:
        """Process a PDF document and extract text, metadata, and images.

        Args:
            content: PDF document bytes
            content_type: MIME type (should be application/pdf)
            filename: Optional filename for better error messages
            options: Processing options (currently unused)
            progress_callback: Optional callback for progress updates

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            ProcessorError: If PDF processing fails
        """
        import anyio

        try:
            if progress_callback:
                await progress_callback(0, 100, "Processing PDF in background thread")

            # Run CPU-bound PDF processing in thread pool to avoid blocking event loop
            result = await anyio.to_thread.run_sync(
                self._process_sync,
                content,
                filename,
            )

            if progress_callback:
                await progress_callback(100, 100, "Processing complete")

            return result

        except Exception as e:
            error_msg = f"Failed to process PDF {filename or '<bytes>'}: {e}"
            logger.error(error_msg, exc_info=True)
            raise ProcessorError(error_msg) from e

    def _process_sync(
        self,
        content: bytes,
        filename: Optional[str] = None,
    ) -> ProcessingResult:
        """Synchronous PDF processing (runs in thread pool).

        Args:
            content: PDF document bytes
            filename: Optional filename for better error messages

        Returns:
            ProcessingResult with extracted text and metadata

        Raises:
            Exception: If PDF processing fails
        """
        # Open PDF from bytes
        doc = pymupdf.open("pdf", content)

        # Extract metadata from PDF
        metadata = self._extract_metadata(doc, filename)

        # Add file size to metadata
        metadata["file_size"] = len(content)

        # Extract text page-by-page to preserve page boundaries
        # pymupdf.layout.activate() causes page_chunks=True to return a string,
        # so we manually extract text per page instead.
        page_boundaries = []
        current_offset = 0
        full_text_parts = []
        image_paths = []

        for page_num in range(doc.page_count):
            if self.extract_images:
                # Generate unique directory for this PDF's images
                pdf_id = filename.replace("/", "_") if filename else "unknown"
                pdf_image_dir = self.image_dir / pdf_id
                pdf_image_dir.mkdir(exist_ok=True, parents=True)

                # Extract page as markdown with images
                page_md = pymupdf4llm.to_markdown(
                    doc,
                    pages=[page_num],  # Extract single page
                    write_images=True,
                    image_path=pdf_image_dir,
                    page_chunks=False,  # Single page, no chunking needed
                )

                # Collect image paths
                if pdf_image_dir.exists():
                    page_images = [str(p) for p in pdf_image_dir.glob("*")]
                    image_paths.extend(page_images)
            else:
                # Extract page as markdown without images
                page_md = pymupdf4llm.to_markdown(
                    doc,
                    pages=[page_num],  # Extract single page
                    write_images=False,
                    page_chunks=False,  # Single page, no chunking needed
                )

            # Store page text
            full_text_parts.append(page_md)

            # Store boundary info: {page (1-indexed), start, end}
            page_boundaries.append(
                {
                    "page": page_num + 1,  # Convert to 1-indexed
                    "start_offset": current_offset,
                    "end_offset": current_offset + len(page_md),
                }
            )

            current_offset += len(page_md)

        # Join all page texts
        md_text = "".join(full_text_parts)

        # Store image metadata
        metadata["has_images"] = len(image_paths) > 0
        if image_paths:
            metadata["image_count"] = len(image_paths)
            metadata["image_paths"] = image_paths

        # Add page boundaries to metadata for chunker to use
        metadata["page_boundaries"] = page_boundaries

        # Close the document
        doc.close()

        logger.info(
            f"Successfully processed PDF {filename or '<bytes>'}: "
            f"{metadata['page_count']} pages, {len(md_text)} chars, "
            f"{metadata.get('image_count', 0)} images"
        )

        return ProcessingResult(
            text=md_text,
            metadata=metadata,
            processor=self.name,
            success=True,
        )

    def _extract_metadata(
        self, doc: pymupdf.Document, filename: Optional[str]
    ) -> dict[str, Any]:
        """Extract metadata from PDF document.

        Args:
            doc: Opened PyMuPDF document
            filename: Optional filename

        Returns:
            Dictionary with PDF metadata
        """
        metadata: dict[str, Any] = {}

        # Basic document info
        metadata["page_count"] = doc.page_count
        metadata["format"] = "PDF 1." + str(
            doc.pdf_version() if hasattr(doc, "pdf_version") else "?"
        )

        if filename:
            metadata["filename"] = filename

        # Extract PDF metadata dictionary
        pdf_metadata = doc.metadata
        if pdf_metadata:
            # Standard PDF metadata fields
            if pdf_metadata.get("title"):
                metadata["title"] = pdf_metadata["title"]
            if pdf_metadata.get("author"):
                metadata["author"] = pdf_metadata["author"]
            if pdf_metadata.get("subject"):
                metadata["subject"] = pdf_metadata["subject"]
            if pdf_metadata.get("keywords"):
                metadata["keywords"] = pdf_metadata["keywords"]
            if pdf_metadata.get("creator"):
                metadata["creator"] = pdf_metadata["creator"]
            if pdf_metadata.get("producer"):
                metadata["producer"] = pdf_metadata["producer"]
            if pdf_metadata.get("creationDate"):
                metadata["creation_date"] = pdf_metadata["creationDate"]
            if pdf_metadata.get("modDate"):
                metadata["modification_date"] = pdf_metadata["modDate"]

        return metadata

    async def health_check(self) -> bool:
        """Check if PyMuPDF is available and working.

        Returns:
            True if processor is ready to use
        """
        try:
            # Try to create a simple PDF in memory
            test_doc = pymupdf.open()
            test_doc.close()
            return True
        except Exception as e:
            logger.error(f"PyMuPDF health check failed: {e}")
            return False
