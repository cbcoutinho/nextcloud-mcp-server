"""Document processor using PyMuPDF (fitz) library."""

import logging
import pathlib
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import anyio

# NOTE: Do NOT call pymupdf.layout.activate() here!
# It changes the behavior of pymupdf4llm.to_markdown() when page_chunks=True,
# causing it to return a string instead of a list[dict].
# See: https://github.com/pymupdf/pymupdf4llm/issues/323
import pymupdf
import pymupdf4llm

from .base import DocumentProcessor, ProcessingResult, ProcessorError

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

        try:
            if progress_callback:
                await progress_callback(0, 100, "Opening PDF document")

            # Open document and extract metadata in thread
            doc = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                lambda: pymupdf.open("pdf", content)
            )

            metadata = self._extract_metadata(doc, filename)
            metadata["file_size"] = len(content)
            page_count = doc.page_count

            if progress_callback:
                await progress_callback(10, 100, f"Extracting {page_count} pages")

            # Prepare image directory if needed
            pdf_image_dir = None
            if self.extract_images:
                pdf_id = filename.replace("/", "_") if filename else "unknown"
                pdf_image_dir = self.image_dir / pdf_id
                pdf_image_dir.mkdir(exist_ok=True, parents=True)

            # Extract all pages in a single call with page_chunks=True
            def do_extract() -> list[dict[str, Any]]:
                # When page_chunks=True, to_markdown returns list[dict] not str
                return pymupdf4llm.to_markdown(  # type: ignore[return-value]
                    doc,
                    write_images=self.extract_images,
                    image_path=pdf_image_dir if self.extract_images else None,
                    page_chunks=True,
                )

            page_chunks: list[dict[str, Any]] = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                do_extract
            )

            if progress_callback:
                await progress_callback(90, 100, "Building result")

            # Extract page texts and build boundaries from chunks
            page_texts: list[str] = []
            page_boundaries: list[dict[str, Any]] = []
            current_offset = 0
            for chunk in page_chunks:
                text = chunk.get("text", "")
                page_num = chunk.get("metadata", {}).get("page", len(page_texts) + 1)
                page_texts.append(text)
                page_boundaries.append(
                    {
                        "page": page_num,
                        "start_offset": current_offset,
                        "end_offset": current_offset + len(text),
                    }
                )
                current_offset += len(text)

            # Collect image paths
            image_paths = []
            if pdf_image_dir and pdf_image_dir.exists():
                image_paths = [str(p) for p in pdf_image_dir.glob("*")]

            # Build final text and metadata
            md_text = "".join(page_texts)
            metadata["has_images"] = len(image_paths) > 0
            if image_paths:
                metadata["image_count"] = len(image_paths)
                metadata["image_paths"] = image_paths
            metadata["page_boundaries"] = page_boundaries

            # Close document
            doc.close()

            if progress_callback:
                await progress_callback(100, 100, "Processing complete")

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

        except Exception as e:
            error_msg = f"Failed to process PDF {filename or '<bytes>'}: {e}"
            logger.error(error_msg, exc_info=True)
            raise ProcessorError(error_msg) from e

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
            doc.pdf_version() if hasattr(doc, "pdf_version") else "?"  # type: ignore[call-non-callable]
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
