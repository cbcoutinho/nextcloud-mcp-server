"""Document processor using PyMuPDF (fitz) library."""

import logging
import pathlib
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import anyio

# pymupdf is used here only for the cheap metadata open. The heavy
# pymupdf4llm.to_markdown extraction runs in an isolated worker subprocess
# (see _isolation.py) so a pathological file can't OOM the pod.
# NOTE: Do NOT call pymupdf.layout.activate()! It changes the behavior of
# pymupdf4llm.to_markdown() when page_chunks=True (returns str, not list[dict]).
# See: https://github.com/pymupdf/pymupdf4llm/issues/323
import pymupdf

from nextcloud_mcp_server.config import get_settings

from ._isolation import PdfParseFailed, run_isolated_pdf_parse
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
                "Initialized PyMuPDFProcessor with image extraction to %s",
                self.image_dir,
            )
        else:
            logger.info("Initialized PyMuPDFProcessor without image extraction")

    @property
    def name(self) -> str:
        return "pymupdf"

    @property
    def tier(self) -> str:
        # pymupdf4llm recovers markdown structure (headings, lists, tables) via
        # the expensive graphics-limited table detection -- it is the
        # ``structured`` escalation target above the pypdfium2 ``fast`` tier.
        return "structured"

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

            # Open document only to read metadata + page count, then close it
            # immediately (try/finally so a failure in _extract_metadata can't
            # leak it). The heavy extraction below works from ``content`` bytes
            # in the isolated worker, so ``doc`` is not needed past this point.
            # Read metadata entirely inside ONE worker thread, serialized against
            # other MuPDF work: pymupdf is not thread-safe, and a doc opened in one
            # thread must not be touched from another. (The heavy page extraction
            # below runs in an isolated subprocess, so it needs no lock.)
            def _read_metadata() -> tuple[dict[str, Any], int]:
                from nextcloud_mcp_server.document_processors._native_locks import (  # noqa: PLC0415
                    pymupdf_serialized,
                )

                with pymupdf_serialized():
                    doc = pymupdf.open("pdf", content)
                    try:
                        meta = self._extract_metadata(doc, filename)
                        meta["file_size"] = len(content)
                        return meta, doc.page_count
                    finally:
                        doc.close()

            metadata, page_count = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                _read_metadata
            )

            if progress_callback:
                await progress_callback(10, 100, f"Extracting {page_count} pages")

            # Prepare image directory if needed
            pdf_image_dir = None
            if self.extract_images:
                pdf_id = filename.replace("/", "_") if filename else "unknown"
                pdf_image_dir = self.image_dir / pdf_id
                pdf_image_dir.mkdir(exist_ok=True, parents=True)

            # Extract all pages (page_chunks=True) in an isolated worker
            # subprocess with a memory rlimit + wall-clock timeout, so a
            # pathological file (e.g. a page with ~1M vector paths that drives
            # table detection past the pod memory limit) fails THIS document
            # instead of OOM-killing the pod. graphics_limit caps per-page
            # vector-graphics analysis (the known trigger).
            settings = get_settings()
            try:
                page_chunks: list[dict[str, Any]] = await run_isolated_pdf_parse(
                    content,
                    write_images=self.extract_images,
                    image_path=pdf_image_dir if self.extract_images else None,
                    graphics_limit=settings.document_pdf_graphics_limit,
                    timeout_seconds=settings.document_parse_timeout_seconds,
                    mem_limit_mb=settings.document_parse_mem_limit_mb,
                )
            except PdfParseFailed as exc:
                logger.warning(
                    "Isolated PDF parse failed for %s (reason=%s): %s",
                    filename or "<bytes>",
                    exc.reason,
                    exc,
                    extra={
                        "processor": self.name,
                        "tier": self.tier,
                        "status": "error",
                        "reason": exc.reason,
                    },
                )
                metadata["parse_failed_reason"] = exc.reason
                return ProcessingResult(
                    text="",
                    metadata=metadata,
                    processor=self.name,
                    success=False,
                    error=f"isolated parse failed ({exc.reason}): {exc}",
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

            if progress_callback:
                await progress_callback(100, 100, "Processing complete")

            logger.info(
                "Successfully processed PDF %s: %s pages, %s chars, %s images",
                filename or "<bytes>",
                metadata["page_count"],
                len(md_text),
                metadata.get("image_count", 0),
                extra={
                    "processor": self.name,
                    "tier": self.tier,
                    "pages": metadata["page_count"],
                    "chars": len(md_text),
                    "images": metadata.get("image_count", 0),
                    "byte_size": len(content),
                },
            )

            return ProcessingResult(
                text=md_text,
                metadata=metadata,
                processor=self.name,
                success=True,
            )

        except Exception as e:
            error_msg = f"Failed to process PDF {filename or '<bytes>'}: {e}"
            logger.error(error_msg)
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
            logger.error("PyMuPDF health check failed: %s", e)
            return False
