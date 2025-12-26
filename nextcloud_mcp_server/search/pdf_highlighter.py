"""PDF chunk highlighting utilities for vector visualization.

This module provides utilities to generate highlighted page images showing
matched chunks and their context from semantic search results.

The highlighting uses character offsets to precisely locate chunks within
PDF documents, ensuring accurate highlighting even when text formatting
varies between indexing and rendering.
"""

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import pymupdf
import pymupdf4llm

logger = logging.getLogger(__name__)


class PDFHighlighter:
    """Generate highlighted page images from PDF chunks."""

    # Color definitions (RGB, 0-1 range)
    COLORS = {
        "yellow": [1, 1, 0],
        "red": [1, 0, 0],
        "green": [0, 1, 0],
        "blue": [0, 0, 1],
        "orange": [1, 0.5, 0],
        "pink": [1, 0, 1],
        "gray": [0.7, 0.7, 0.7],
        "light_blue": [0.7, 0.9, 1.0],
        "light_green": [0.7, 1.0, 0.7],
    }

    @staticmethod
    def strip_markdown(text: str) -> str:
        """Remove markdown formatting to improve search accuracy.

        Args:
            text: Text with potential markdown formatting

        Returns:
            Plain text with markdown removed
        """
        # Remove bold/italic markers
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        text = re.sub(r"_(.+?)_", r"\1", text)

        # Remove headers
        text = re.sub(r"^#+\s+", "", text, flags=re.MULTILINE)

        # Remove inline code
        text = re.sub(r"`(.+?)`", r"\1", text)

        return text.strip()

    @staticmethod
    def extract_pdf_text_with_boundaries(
        pdf_doc: pymupdf.Document,
    ) -> tuple[str, list[dict]]:
        """Extract full document text with page boundary tracking.

        Uses pymupdf4llm.to_markdown() for consistency with indexing.

        IMPORTANT: Must use write_images=True to match PyMuPDFProcessor behavior!
        Even though we don't need the images, we need the image references in the
        markdown text to maintain consistent character offsets with indexing.

        Args:
            pdf_doc: Open PyMuPDF document

        Returns:
            Tuple of (full_text, page_boundaries) where page_boundaries is a list of:
            {"page": 1, "start_offset": 0, "end_offset": 1234}
        """

        page_boundaries = []
        text_parts = []
        current_offset = 0

        # Use temp directory for image output (images are discarded after extraction)
        temp_dir = Path(tempfile.mkdtemp(prefix="pdf_highlight_"))

        for page_idx in range(pdf_doc.page_count):
            page_md = pymupdf4llm.to_markdown(
                pdf_doc,
                pages=[page_idx],
                write_images=True,  # Must match indexing! Otherwise offsets misalign
                image_path=temp_dir,
                page_chunks=False,
            )

            page_boundaries.append(
                {
                    "page": page_idx + 1,  # 1-indexed
                    "start_offset": current_offset,
                    "end_offset": current_offset + len(page_md),
                }
            )

            text_parts.append(page_md)
            current_offset += len(page_md)

        full_text = "".join(text_parts)

        # Clean up temp directory and extracted images

        try:
            shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Failed to clean up temp directory {temp_dir}: {e}")

        return full_text, page_boundaries

    @staticmethod
    def find_chunk_page(
        chunk_start_offset: int,
        chunk_end_offset: int,
        page_boundaries: list[dict],
    ) -> Optional[dict]:
        """Find which page contains the most of a given chunk.

        Args:
            chunk_start_offset: Chunk start position in full document
            chunk_end_offset: Chunk end position in full document
            page_boundaries: Page boundary list from extract_pdf_text_with_boundaries()

        Returns:
            Dict with keys: page_num, overlap_chars, page_relative_start, page_relative_end
            or None if chunk not found on any page
        """
        chunk_pages = []

        for boundary in page_boundaries:
            page_start = boundary["start_offset"]
            page_end = boundary["end_offset"]

            # Check if chunk overlaps with this page
            if chunk_start_offset < page_end and chunk_end_offset > page_start:
                overlap_start = max(chunk_start_offset, page_start)
                overlap_end = min(chunk_end_offset, page_end)
                overlap_chars = overlap_end - overlap_start

                chunk_pages.append(
                    {
                        "page_num": boundary["page"],
                        "overlap_chars": overlap_chars,
                        "page_relative_start": overlap_start - page_start,
                        "page_relative_end": overlap_end - page_start,
                    }
                )

        if not chunk_pages:
            return None

        # Return page with maximum overlap
        return max(chunk_pages, key=lambda p: p["overlap_chars"])

    @staticmethod
    def highlight_chunk_by_word_positions(
        page: pymupdf.Page,
        chunk_text: str,
        color: str = "yellow",
        search_region: tuple[float, float, float, float] | None = None,
    ) -> int:
        """Highlight chunk using word-position matching.

        This method matches words from the chunk to their positions on the PDF page,
        avoiding text search mismatches between markdown-formatted text and raw PDF text.

        Args:
            page: PyMuPDF page object
            chunk_text: Text to highlight (may contain markdown)
            color: Color name from COLORS dict
            search_region: Optional (x0, y0, x1, y1) bounding box to constrain search.
                          If provided, only words within this region are considered.

        Returns:
            Number of highlight rectangles added
        """
        # Tokenize chunk into words (alphanumeric only, lowercase)
        chunk_words = re.findall(
            r"\w+", PDFHighlighter.strip_markdown(chunk_text).lower()
        )

        if not chunk_words:
            logger.warning("No words found in chunk text")
            return 0

        # Get all words from page with positions
        # Format: (x0, y0, x1, y1, "word", block_no, line_no, word_no)
        try:
            page_words = page.get_text("words")
        except Exception as e:
            logger.error(f"Failed to extract words from page: {e}")
            return 0

        if not page_words:
            logger.warning("No words found on page")
            return 0

        # Filter words by search region if provided
        if search_region:
            rx0, ry0, rx1, ry1 = search_region
            # Allow some tolerance (10 points) for words near region boundary
            tolerance = 10
            page_words = [
                w
                for w in page_words
                if (
                    w[0] >= rx0 - tolerance
                    and w[2] <= rx1 + tolerance
                    and w[1] >= ry0 - tolerance
                    and w[3] <= ry1 + tolerance
                )
            ]
            logger.debug(
                f"Filtered to {len(page_words)} words in region "
                f"({rx0:.0f}, {ry0:.0f}, {rx1:.0f}, {ry1:.0f})"
            )

        if not page_words:
            logger.warning("No words found in search region")
            return 0

        # Find matching word sequence - use FIRST match, not longest
        # This ensures we highlight the actual chunk location, not similar text elsewhere
        matches = []

        # Build a simple word-to-positions index for the first few chunk words
        # to find candidate starting positions
        first_chunk_word = chunk_words[0] if chunk_words else ""
        candidate_starts = []

        for i, pw in enumerate(page_words):
            page_word = pw[4].lower()
            # Check if this could be the start of the chunk
            if (
                first_chunk_word == page_word
                or first_chunk_word in page_word
                or page_word in first_chunk_word
            ):
                candidate_starts.append(i)

        # Try each candidate start position and take the FIRST good match
        for start_pos in candidate_starts:
            current_matches = []
            chunk_idx = 0
            skip_count = 0
            max_skips = 3  # Allow some formatting differences

            for page_idx in range(start_pos, len(page_words)):
                if chunk_idx >= len(chunk_words):
                    break

                page_word = page_words[page_idx][4].lower()
                chunk_word = chunk_words[chunk_idx]

                # Check for match (allow partial matches for flexibility)
                if (
                    chunk_word == page_word
                    or chunk_word in page_word
                    or page_word in chunk_word
                ):
                    current_matches.append(page_words[page_idx])
                    chunk_idx += 1
                    skip_count = 0
                elif skip_count < max_skips:
                    # Allow skipping some words (formatting, punctuation)
                    skip_count += 1
                    continue
                else:
                    break

            # Accept if we matched at least 50% of chunk words
            if len(current_matches) >= len(chunk_words) * 0.5:
                matches = current_matches
                logger.debug(
                    f"Found match at position {start_pos}: "
                    f"{len(matches)}/{len(chunk_words)} words"
                )
                break  # Take FIRST match, not best/longest

        if not matches:
            logger.debug(f"No word matches found (chunk has {len(chunk_words)} words)")
            return 0

        logger.debug(
            f"Matched {len(matches)} words out of {len(chunk_words)} chunk words"
        )

        # Build rectangles from matched words
        rects = [pymupdf.Rect(w[0], w[1], w[2], w[3]) for w in matches]

        # Check if matches are contiguous (not scattered across the page)
        # Scattered matches indicate false positives from common words
        if len(rects) > 1:
            # Sort by vertical position then horizontal
            sorted_matches = sorted(matches, key=lambda w: (round(w[1]), w[0]))

            # Check for large vertical gaps (more than ~2 lines apart)
            # A typical line height is 12-20 points
            max_line_gap = 50  # Points - allows for ~2-3 lines gap
            prev_y = sorted_matches[0][1]
            large_gaps = 0

            for match in sorted_matches[1:]:
                y_gap = match[1] - prev_y
                if y_gap > max_line_gap:
                    large_gaps += 1
                prev_y = match[1]

            # If matches are scattered (many large gaps), reject this match
            # A chunk should be mostly contiguous text
            if large_gaps > len(matches) * 0.3:  # More than 30% have gaps
                logger.debug(
                    f"Rejecting scattered matches: {large_gaps} large gaps "
                    f"out of {len(matches)} matches"
                )
                return 0

        # Merge adjacent rectangles on the same line for cleaner highlighting
        merged_rects = []
        sorted_rects = sorted(rects, key=lambda r: (round(r.y0), r.x0))

        current_rect = None
        for rect in sorted_rects:
            if current_rect is None:
                current_rect = rect
            elif abs(rect.y0 - current_rect.y0) < 5:  # Same line (within 5 points)
                current_rect = current_rect | rect  # Union
            else:
                merged_rects.append(current_rect)
                current_rect = rect

        if current_rect:
            merged_rects.append(current_rect)

        # Add highlights
        rgb = PDFHighlighter.COLORS.get(color, PDFHighlighter.COLORS["yellow"])
        for rect in merged_rects:
            highlight = page.add_highlight_annot(rect)
            highlight.set_colors({"stroke": rgb})
            highlight.set_info(
                content="Chunk from semantic search",
                title="PDF Highlighter (word-position)",
            )
            highlight.update()

        return len(merged_rects)

    @staticmethod
    def find_unique_phrase(
        text: str, min_len: int = 30, max_len: int = 80
    ) -> str | None:
        """Find a relatively unique phrase from text for location search.

        Looks for phrases that are likely to be unique on the page:
        - Prefers phrases with numbers or special terms
        - Avoids very common words

        Args:
            text: Source text to extract phrase from
            min_len: Minimum phrase length
            max_len: Maximum phrase length

        Returns:
            A phrase likely to be unique, or None if not found
        """
        clean_text = PDFHighlighter.strip_markdown(text).strip()
        if not clean_text:
            return None

        # Try first sentence (often unique due to context)
        sentences = re.split(r"[.!?]\s+", clean_text)
        for sentence in sentences:
            sentence = sentence.strip()
            if min_len <= len(sentence) <= max_len:
                return sentence
            elif len(sentence) > max_len:
                return sentence[:max_len]

        # Fallback: first N chars
        if len(clean_text) >= min_len:
            return clean_text[:max_len]

        return clean_text if clean_text else None

    @staticmethod
    def _find_chunk_bbox(
        page: pymupdf.Page,
        chunk_text: str,
        page_relative_start: int,
        page_relative_end: int,
        page_text_length: int,
    ) -> tuple[float, float, float, float] | None:
        """Find bounding box for a chunk without modifying the page.

        Returns (x0, y0, x1, y1) in page coordinates, or None if not found.
        """
        page_rect = page.rect

        # Strip markdown for searching
        search_text = PDFHighlighter.strip_markdown(chunk_text)

        # Try to find chunk location using text search
        anchor_rect = None
        search_phrases = []

        # Build search phrases from chunk text
        sentences = re.split(r"[.!?]\s+", search_text)
        for sentence in sentences[:3]:
            sentence = sentence.strip()
            if len(sentence) >= 20:
                search_phrases.append(sentence[:80])
                if len(sentence) >= 40:
                    search_phrases.append(sentence[:40])

        # Also try first N characters
        if len(search_text) >= 30:
            search_phrases.append(search_text[:60])
            search_phrases.append(search_text[:30])

        for phrase in search_phrases:
            if not phrase:
                continue
            rects = page.search_for(phrase.strip())
            if rects:
                anchor_rect = rects[0]
                break

        if not anchor_rect:
            return None

        # Calculate chunk height based on character count
        chunk_chars = len(search_text)
        estimated_lines = max(1, chunk_chars / 60)
        estimated_height = estimated_lines * 14

        # Build bounding box
        return (
            page_rect.x0 + 30,  # Left margin
            anchor_rect.y0 - 5,  # Start slightly above anchor
            page_rect.x1 - 30,  # Right margin
            min(anchor_rect.y0 + estimated_height + 10, page_rect.y1 - 30),
        )

    @staticmethod
    def highlight_chunk_on_page(
        page: pymupdf.Page,
        chunk_text: str,
        color: str = "yellow",
        page_relative_start: int | None = None,
        page_relative_end: int | None = None,
        page_text_length: int | None = None,
    ) -> int:
        """Add bounding box highlight to a PDF page for the given chunk text.

        Uses text search to find the chunk's location on the page, then draws
        a bounding box around that region. Falls back to character offset estimation
        if text search fails.

        Args:
            page: PyMuPDF page object
            chunk_text: Text to highlight (may contain markdown)
            color: Color name from COLORS dict
            page_relative_start: Character offset where chunk starts on page (optional)
            page_relative_end: Character offset where chunk ends on page (optional)
            page_text_length: Total character length of page text (optional)

        Returns:
            Number of highlights added (1 for bounding box, 0 if failed)
        """
        page_rect = page.rect
        rgb = PDFHighlighter.COLORS.get(color, PDFHighlighter.COLORS["yellow"])

        # Strip markdown for searching
        search_text = PDFHighlighter.strip_markdown(chunk_text)

        # Try to find chunk location using text search
        # Search for progressively shorter phrases until we find a match
        anchor_rect = None
        search_phrases = []

        # Build search phrases from chunk text
        sentences = re.split(r"[.!?]\s+", search_text)
        for sentence in sentences[:3]:  # Try first 3 sentences
            sentence = sentence.strip()
            if len(sentence) >= 20:
                search_phrases.append(sentence[:80])
                if len(sentence) >= 40:
                    search_phrases.append(sentence[:40])

        # Also try first N characters
        if len(search_text) >= 30:
            search_phrases.append(search_text[:60])
            search_phrases.append(search_text[:30])

        for phrase in search_phrases:
            if not phrase:
                continue
            rects = page.search_for(phrase.strip())
            if rects:
                anchor_rect = rects[0]  # Use first match
                logger.debug(f"Found chunk anchor using phrase: '{phrase[:30]}...'")
                break

        if not anchor_rect:
            page_num = page.number + 1 if page.number is not None else "unknown"
            logger.warning(f"Could not find chunk text on page {page_num}")
            return 0

        # Calculate chunk height based on character count
        # Estimate ~15 chars per line, ~12pt line height
        chunk_chars = len(search_text)
        estimated_lines = max(1, chunk_chars / 60)  # ~60 chars per line typical
        estimated_height = estimated_lines * 14  # ~14pt per line

        # Build bounding box starting from anchor
        chunk_rect = pymupdf.Rect(
            page_rect.x0 + 30,  # Left margin
            anchor_rect.y0 - 5,  # Start slightly above anchor
            page_rect.x1 - 30,  # Right margin
            min(
                anchor_rect.y0 + estimated_height + 10, page_rect.y1 - 30
            ),  # Estimated bottom
        )

        # Draw a visible rectangle around the chunk region
        shape = page.new_shape()
        shape.draw_rect(chunk_rect)
        shape.finish(
            color=rgb,  # Border color
            fill=None,  # No fill (transparent)
            width=2.5,  # Border width
            dashes="[4 2]",  # Dashed line
        )
        shape.commit()

        # Add semi-transparent fill for visibility
        fill_shape = page.new_shape()
        fill_shape.draw_rect(chunk_rect)
        fill_shape.finish(
            color=None,  # No border
            fill=[1, 1, 0.7],  # Light yellow fill
            fill_opacity=0.15,  # Very transparent
        )
        fill_shape.commit()

        logger.debug(
            f"Added bounding box at y={chunk_rect.y0:.0f}-{chunk_rect.y1:.0f} "
            f"(estimated {estimated_lines:.1f} lines)"
        )

        return 1

    @staticmethod
    def highlight_chunk(
        pdf_bytes: bytes,
        chunk_start_offset: int,
        chunk_end_offset: int,
        stored_page_number: Optional[int] = None,
        color: str = "yellow",
        zoom: float = 2.0,
    ) -> Optional[tuple[bytes, int, int]]:
        """Generate PNG image of PDF page with highlighted chunk.

        This is the main entry point for highlighting. It:
        1. Extracts document text with page boundaries
        2. Finds which page contains the chunk
        3. Extracts chunk text using character offsets
        4. Highlights the chunk on the page
        5. Renders page to PNG

        Args:
            pdf_bytes: PDF file bytes
            chunk_start_offset: Chunk start position (document-level)
            chunk_end_offset: Chunk end position (document-level)
            stored_page_number: Page number from metadata (optional, for validation)
            color: Highlight color name
            zoom: Rendering zoom factor (2.0 = 144 DPI)

        Returns:
            Tuple of (png_bytes, page_number, highlight_count) or None if failed
        """

        temp_pdf_path = None
        try:
            # Write PDF to temp file with consistent name "pdf.pdf"
            # This ensures image references match indexing (e.g., pdf-0001.png)
            # Different temp filenames would cause different markdown text lengths!
            temp_dir = Path(tempfile.mkdtemp(prefix="pdf_highlight_"))
            temp_pdf_path = temp_dir / "pdf.pdf"
            temp_pdf_path.write_bytes(pdf_bytes)

            # Open PDF from temp file
            doc = pymupdf.open(temp_pdf_path)

            # Extract text with page boundaries
            full_text, page_boundaries = (
                PDFHighlighter.extract_pdf_text_with_boundaries(doc)
            )

            # Find which page contains the chunk
            chunk_page_info = PDFHighlighter.find_chunk_page(
                chunk_start_offset, chunk_end_offset, page_boundaries
            )

            if not chunk_page_info:
                logger.error("Chunk not found on any page")
                doc.close()
                return None

            page_num = chunk_page_info["page_num"]

            # Log if page differs from stored metadata
            if stored_page_number and stored_page_number != page_num:
                logger.info(
                    f"Chunk primarily on page {page_num}, metadata says {stored_page_number}"
                )

            # Extract page text
            page_boundary = page_boundaries[page_num - 1]
            page_start = page_boundary["start_offset"]
            page_end = page_boundary["end_offset"]
            page_text = full_text[page_start:page_end]

            # Extract chunk text using page-relative offsets
            page_relative_start = chunk_page_info["page_relative_start"]
            page_relative_end = chunk_page_info["page_relative_end"]
            chunk_text = page_text[page_relative_start:page_relative_end]

            # Calculate page text length for region estimation
            page_text_length = page_end - page_start

            logger.debug(
                f"Extracted {len(chunk_text)} chars on page {page_num} "
                f"(offsets {page_relative_start}-{page_relative_end} of {page_text_length})"
            )

            # Get page and add highlights
            page = doc[page_num - 1]
            highlight_count = PDFHighlighter.highlight_chunk_on_page(
                page,
                chunk_text,
                color,
                page_relative_start=page_relative_start,
                page_relative_end=page_relative_end,
                page_text_length=page_text_length,
            )

            if highlight_count == 0:
                logger.warning("No highlights added")
                doc.close()
                return None

            # Render page to PNG
            mat = pymupdf.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            png_bytes = pix.tobytes("png")

            doc.close()

            logger.info(
                f"Generated {len(png_bytes):,} byte image with {highlight_count} highlights"
            )

            return (png_bytes, page_num, highlight_count)

        except Exception as e:
            logger.error(f"Error highlighting chunk: {e}", exc_info=True)
            return None

        finally:
            # Clean up temp directory and PDF file
            if temp_pdf_path and temp_pdf_path.parent.exists():
                try:
                    import shutil

                    shutil.rmtree(temp_pdf_path.parent)
                except Exception as e:
                    logger.warning(
                        f"Failed to delete temp directory {temp_pdf_path.parent}: {e}"
                    )

    @staticmethod
    def highlight_chunks_batch(
        pdf_bytes: bytes,
        chunks: list[tuple[int, int, int, int | None, str]],
        page_boundaries: list[dict],
        full_text: str,
        color: str = "yellow",
        zoom: float = 2.0,
    ) -> dict[int, tuple[bytes, int, int]]:
        """Generate highlighted images for multiple chunks.

        Opens PDF once for rendering, uses pre-computed page boundaries from the
        document processor. This ensures consistent character offsets between
        chunking and highlighting.

        Args:
            pdf_bytes: PDF file bytes
            chunks: List of (chunk_index, start_offset, end_offset, stored_page_number, chunk_text)
                    The chunk_index is used as the key in the returned dict.
                    chunk_text is the actual text content of the chunk.
            page_boundaries: Pre-computed page boundaries from document processor.
                            Each entry: {"page": 1, "start_offset": 0, "end_offset": 1234}
            full_text: Full document text for extracting page-relative portions.
            color: Highlight color name
            zoom: Rendering zoom factor (2.0 = 144 DPI)

        Returns:
            Dict mapping chunk_index to (png_bytes, page_number, highlight_count)
            Chunks that fail to highlight are omitted from the result.
        """
        import shutil
        import tempfile
        from collections import defaultdict
        from pathlib import Path

        results: dict[int, tuple[bytes, int, int]] = {}

        if not chunks:
            return results

        temp_pdf_path = None
        try:
            # Write PDF to temp file
            temp_dir = Path(tempfile.mkdtemp(prefix="pdf_highlight_batch_"))
            temp_pdf_path = temp_dir / "pdf.pdf"
            temp_pdf_path.write_bytes(pdf_bytes)

            # Open PDF once (only for rendering, not text extraction)
            doc = pymupdf.open(temp_pdf_path)

            logger.debug(
                f"Batch highlighting: {len(chunks)} chunks, "
                f"{len(page_boundaries)} pages"
            )

            # Group chunks by their target page for efficient rendering
            # We'll render each page only once with all its highlights
            chunks_by_page: dict[int, list[tuple[int, dict, str]]] = defaultdict(list)

            for chunk_tuple in chunks:
                # Unpack chunk tuple - chunk_text is now passed directly
                chunk_index, start_offset, end_offset, stored_page_num, chunk_text = (
                    chunk_tuple
                )

                # Find which page contains this chunk
                chunk_page_info = PDFHighlighter.find_chunk_page(
                    start_offset, end_offset, page_boundaries
                )

                if not chunk_page_info:
                    logger.warning(f"Chunk {chunk_index}: not found on any page")
                    continue

                page_num = chunk_page_info["page_num"]

                # Log if page differs from stored metadata
                if stored_page_num and stored_page_num != page_num:
                    logger.debug(
                        f"Chunk {chunk_index}: found on page {page_num}, "
                        f"metadata says {stored_page_num}"
                    )

                # Extract page-relative portion of chunk text
                # This is critical for cross-page chunks where the start
                # of the chunk might be on a different page
                page_boundary = page_boundaries[page_num - 1]
                page_start = page_boundary["start_offset"]
                page_end = page_boundary["end_offset"]
                page_text_length = page_end - page_start

                # Calculate what portion of the chunk appears on this page
                chunk_start_on_page = max(start_offset, page_start)
                chunk_end_on_page = min(end_offset, page_end)

                # Extract just the text that appears on this page
                page_relative_text = full_text[chunk_start_on_page:chunk_end_on_page]

                chunks_by_page[page_num].append(
                    (chunk_index, chunk_page_info, page_relative_text, page_text_length)
                )

            logger.debug(
                f"Chunks distributed across {len(chunks_by_page)} unique pages"
            )

            # OPTIMIZATION: Render each page ONCE, then draw highlights using PIL
            # This avoids expensive page.get_pixmap() calls per chunk
            from io import BytesIO

            from PIL import Image, ImageDraw

            # PIL color for bounding box (RGB tuple)
            rgb = PDFHighlighter.COLORS.get(color, PDFHighlighter.COLORS["yellow"])
            pil_color = tuple(int(c * 255) for c in rgb)
            fill_color = (255, 255, 178, 38)  # Light yellow with alpha

            for page_num, page_chunks in chunks_by_page.items():
                page = doc[page_num - 1]

                # Render page ONCE to get base image (most expensive operation)
                mat = pymupdf.Matrix(zoom, zoom)
                base_pix = page.get_pixmap(matrix=mat, alpha=False)
                base_png = base_pix.tobytes("png")

                # Convert to PIL Image for fast highlight drawing
                base_image = Image.open(BytesIO(base_png)).convert("RGBA")
                page_rect = page.rect

                logger.debug(
                    f"Page {page_num}: rendered once, processing {len(page_chunks)} chunks"
                )

                for (
                    chunk_index,
                    chunk_page_info,
                    chunk_text,
                    page_text_length,
                ) in page_chunks:
                    try:
                        # Find chunk bounding box using text search
                        bbox = PDFHighlighter._find_chunk_bbox(
                            page,
                            chunk_text,
                            chunk_page_info["page_relative_start"],
                            chunk_page_info["page_relative_end"],
                            page_text_length,
                        )

                        if bbox is None:
                            logger.warning(f"Chunk {chunk_index}: could not find bbox")
                            continue

                        # Copy base image for this chunk
                        chunk_image = base_image.copy()

                        # Scale bbox coordinates to pixmap coordinates
                        scale_x = base_pix.width / page_rect.width
                        scale_y = base_pix.height / page_rect.height
                        pil_bbox = (
                            int(bbox[0] * scale_x),
                            int(bbox[1] * scale_y),
                            int(bbox[2] * scale_x),
                            int(bbox[3] * scale_y),
                        )

                        # Create transparent overlay for fill (proper alpha blending)
                        overlay = Image.new("RGBA", chunk_image.size, (0, 0, 0, 0))
                        overlay_draw = ImageDraw.Draw(overlay)
                        overlay_draw.rectangle(pil_bbox, fill=fill_color)

                        # Alpha composite the overlay onto the chunk image
                        chunk_image = Image.alpha_composite(chunk_image, overlay)

                        # Draw border on top (solid, not transparent)
                        border_draw = ImageDraw.Draw(chunk_image)
                        border_draw.rectangle(pil_bbox, outline=pil_color, width=3)

                        # Convert back to PNG bytes
                        output = BytesIO()
                        chunk_image.convert("RGB").save(output, format="PNG")
                        png_bytes = output.getvalue()

                        results[chunk_index] = (png_bytes, page_num, 1)

                        logger.debug(
                            f"Chunk {chunk_index}: {len(png_bytes):,} bytes, "
                            f"page {page_num}, bbox {pil_bbox}"
                        )

                    except Exception as e:
                        logger.error(f"Chunk {chunk_index}: error - {e}")
                        continue

            doc.close()

            logger.info(
                f"Batch highlighted {len(results)}/{len(chunks)} chunks successfully"
            )

            return results

        except Exception as e:
            logger.error(f"Error in batch highlighting: {e}", exc_info=True)
            return results

        finally:
            # Clean up temp directory
            if temp_pdf_path and temp_pdf_path.parent.exists():
                try:
                    shutil.rmtree(temp_pdf_path.parent)
                except Exception as e:
                    logger.warning(f"Failed to clean up temp dir: {e}")
