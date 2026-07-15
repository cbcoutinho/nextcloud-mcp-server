"""Unit tests for the per-page word cache in compute_chunk_bboxes_batch.

The bbox path previously re-ran PyMuPDF ``page.get_text("words")`` +
tokenisation once per chunk (the ingest worker's #1 CPU hotspot). It now
extracts each page's words once and reuses them across that page's chunks.
"""

from __future__ import annotations

import pymupdf
import pytest

from nextcloud_mcp_server.search.pdf_highlighter import PDFHighlighter


def _make_pdf(pages: list[str]) -> bytes:
    """Build an in-memory PDF whose pages contain the given text."""
    doc = pymupdf.open()
    for body in pages:
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((50, 50), body)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _page_boundaries(pages: list[str]) -> tuple[list[dict], str]:
    """Build (page_boundaries, full_text) compatible with the highlighter API."""
    boundaries: list[dict] = []
    cursor = 0
    parts: list[str] = []
    for i, body in enumerate(pages, start=1):
        end = cursor + len(body)
        boundaries.append({"page": i, "start_offset": cursor, "end_offset": end})
        parts.append(body)
        cursor = end
    return boundaries, "".join(parts)


@pytest.mark.unit
def test_page_words_extracted_once_per_page_not_per_chunk(mocker):
    """``_page_flat_tokens`` runs once per distinct page, not once per chunk."""
    pages = [
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
        "nu xi omicron pi rho sigma tau upsilon phi chi psi omega one two three.",
        "red orange yellow green blue indigo violet black white gray brown "
        "cyan magenta teal navy olive maroon silver gold coral salmon plum.",
    ]
    pdf_bytes = _make_pdf(pages)
    boundaries, full_text = _page_boundaries(pages)
    l1 = len(pages[0])
    l2 = len(pages[1])

    # 3 chunks fully inside page 1, 2 chunks fully inside page 2.
    chunks = [
        (0, 0, l1 // 3, 1, full_text[0 : l1 // 3]),
        (1, l1 // 3, 2 * l1 // 3, 1, full_text[l1 // 3 : 2 * l1 // 3]),
        (2, 2 * l1 // 3, l1, 1, full_text[2 * l1 // 3 : l1]),
        (3, l1, l1 + l2 // 2, 2, full_text[l1 : l1 + l2 // 2]),
        (4, l1 + l2 // 2, l1 + l2, 2, full_text[l1 + l2 // 2 : l1 + l2]),
    ]

    spy = mocker.spy(PDFHighlighter, "_page_flat_tokens")

    PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=full_text,
    )

    # 5 chunks across 2 pages -> extraction runs twice (once per page), not 5x.
    assert spy.call_count == 2


@pytest.mark.unit
def test_flat_cache_yields_identical_rects_to_recompute():
    """Passing a precomputed flat list matches recomputing it per call."""
    pages = [
        "The quick brown fox jumps over the lazy dog near the river bank "
        "while the sun sets slowly behind the distant rolling green hills.",
    ]
    pdf_bytes = _make_pdf(pages)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[0]
        flat = PDFHighlighter._page_flat_tokens(page)
        for chunk_text in (
            "quick brown fox jumps over the lazy dog",
            "sun sets slowly behind the distant rolling green hills",
            "nonexistent words that will not match anything here",
        ):
            with_flat = PDFHighlighter._find_chunk_line_rects(
                page, chunk_text, flat=flat
            )
            recomputed = PDFHighlighter._find_chunk_line_rects(page, chunk_text)
            assert with_flat == recomputed
    finally:
        doc.close()


@pytest.mark.unit
def test_empty_page_flat_tokens_is_cached_and_reused(mocker):
    """A page with no matchable words still extracts once and reuses the [] result."""
    pages = ["   "]  # whitespace-only page -> empty word list
    pdf_bytes = _make_pdf(pages)
    boundaries, full_text = _page_boundaries(pages)
    l1 = len(pages[0])
    chunks = [
        (0, 0, l1 // 2, 1, full_text[0 : l1 // 2]),
        (1, l1 // 2, l1, 1, full_text[l1 // 2 : l1]),
    ]

    spy = mocker.spy(PDFHighlighter, "_page_flat_tokens")
    results = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=full_text,
    )

    # Two chunks, one page: extracted once, empty result cached and reused.
    assert spy.call_count == 1
    assert results == {}
