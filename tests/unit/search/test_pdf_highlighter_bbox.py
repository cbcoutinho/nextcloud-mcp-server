"""Unit tests for PDFHighlighter.compute_chunk_bboxes_batch (Deck #76).

Replaces the legacy `highlight_chunks_batch`-+-base64 pipeline that inflated
Qdrant payloads with per-chunk PNG screenshots. The new path returns
normalized bounding boxes only.
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
def test_compute_chunk_bboxes_returns_normalized_rects():
    """Each returned bbox should be 4 floats in [0, 1] tagged with the page."""
    pages = [
        "Chapter 1: Introduction. Nextcloud is a self-hosted collaboration platform "
        "covering installation, configuration and maintenance topics.",
        "Chapter 2: Installation. Download the package, extract it to the web "
        "server directory, and configure the database connection.",
    ]
    pdf_bytes = _make_pdf(pages)
    boundaries, full_text = _page_boundaries(pages)

    chunks = [
        (
            0,
            0,
            len(pages[0]),
            1,
            "Chapter 1: Introduction. Nextcloud is a self-hosted collaboration platform.",
        ),
        (
            1,
            len(pages[0]),
            len(pages[0]) + len(pages[1]),
            2,
            "Chapter 2: Installation. Download the package.",
        ),
    ]

    results = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=full_text,
    )

    assert set(results) == {0, 1}

    bboxes_p1, page_p1 = results[0]
    bboxes_p2, page_p2 = results[1]

    assert page_p1 == 1
    assert page_p2 == 2

    for rects in (bboxes_p1, bboxes_p2):
        assert len(rects) >= 1
        for rect in rects:
            assert len(rect) == 4
            x0, y0, x1, y1 = rect
            assert 0.0 <= x0 < x1 <= 1.0
            assert 0.0 <= y0 < y1 <= 1.0


@pytest.mark.unit
def test_compute_chunk_bboxes_empty_input():
    assert (
        PDFHighlighter.compute_chunk_bboxes_batch(
            pdf_bytes=b"",
            chunks=[],
            page_boundaries=[],
            full_text="",
        )
        == {}
    )


@pytest.mark.unit
def test_compute_chunk_bboxes_omits_when_offsets_out_of_range():
    """Chunks whose offsets fall outside every page boundary are omitted.

    Verifies the docstring contract: *"Chunks whose bbox cannot be located
    are omitted from the result."* (path: ``find_chunk_page`` returns None).
    """
    pages = ["Page one body text content here for the test."]
    pdf_bytes = _make_pdf(pages)
    boundaries, full_text = _page_boundaries(pages)

    # Offsets way beyond the document end — no page boundary matches.
    out_of_range_start = len(full_text) + 1000
    out_of_range_end = out_of_range_start + 50
    chunks = [(0, out_of_range_start, out_of_range_end, 1, "irrelevant")]

    results = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=full_text,
    )

    assert results == {}


@pytest.mark.unit
def test_compute_chunk_bboxes_omits_when_text_not_in_pdf():
    """Chunks whose page-relative text isn't on the page are omitted.

    Verifies the second omission path: ``_find_chunk_bbox`` returns None
    when the supplied text cannot be located on the rendered page.
    """
    pages = ["Hello world."]
    pdf_bytes = _make_pdf(pages)
    # Build boundaries from the real text but pass a *different* full_text
    # so the page-relative slice is content that does not exist in the PDF.
    boundaries, _ = _page_boundaries(pages)
    bogus_full_text = "Z" * len(pages[0])

    chunks = [(0, 0, len(pages[0]), 1, "ignored")]

    results = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=bogus_full_text,
    )

    assert results == {}


@pytest.mark.unit
@pytest.mark.parametrize("page_index", [0, 1])
def test_compute_chunk_bboxes_assigns_correct_page(page_index: int):
    """Verify the page number returned matches the page the chunk lives on."""
    pages = [
        "Page one talks about apples and oranges in detail.",
        "Page two discusses bananas and grapes thoroughly.",
    ]
    pdf_bytes = _make_pdf(pages)
    boundaries, full_text = _page_boundaries(pages)

    if page_index == 0:
        chunk_text = "apples and oranges"
        offsets = (0, len(pages[0]))
    else:
        chunk_text = "bananas and grapes"
        offsets = (len(pages[0]), len(pages[0]) + len(pages[1]))

    chunks = [(0, offsets[0], offsets[1], page_index + 1, chunk_text)]

    results = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=full_text,
    )

    assert results, "expected a bbox for the chunk"
    _, page_num = results[0]
    assert page_num == page_index + 1


@pytest.mark.unit
def test_compute_chunk_bboxes_handles_unordered_page_boundaries():
    """Page lookup must match by ``page`` key, not by list position.

    Regression guard: an earlier implementation indexed
    ``page_boundaries[page_num - 1]``, which silently produces a wrong
    bbox if boundaries are passed out of order. Reverse the boundaries
    and assert the result is identical to the in-order case.
    """
    pages = [
        "Page one talks about apples and oranges in detail.",
        "Page two discusses bananas and grapes thoroughly.",
    ]
    pdf_bytes = _make_pdf(pages)
    boundaries, full_text = _page_boundaries(pages)

    chunks = [
        (0, 0, len(pages[0]), 1, "apples and oranges"),
        (
            1,
            len(pages[0]),
            len(pages[0]) + len(pages[1]),
            2,
            "bananas and grapes",
        ),
    ]

    in_order = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=full_text,
    )
    reversed_order = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=list(reversed(boundaries)),
        full_text=full_text,
    )

    assert in_order == reversed_order
    assert reversed_order[0][1] == 1
    assert reversed_order[1][1] == 2


@pytest.mark.unit
def test_chunk_bbox_covers_multiple_lines():
    """A chunk spanning several lines yields one tight rect PER LINE, not a single
    estimated-height box (the #404 pymupdf-path geometry fix)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = ["Alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu"]
    for i, ln in enumerate(lines):
        page.insert_text((50, 100 + i * 30), ln)
    pdf_bytes = doc.tobytes()
    doc.close()

    body = " ".join(lines)
    boundaries = [{"page": 1, "start_offset": 0, "end_offset": len(body)}]
    chunks = [(0, 0, len(body), 1, body)]

    results = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes, chunks=chunks, page_boundaries=boundaries, full_text=body
    )
    rects, page_num = results[0]
    assert page_num == 1
    # one rect per text line (3), vertically separated, each valid + normalized
    assert len(rects) >= 3
    y0s = sorted(r[1] for r in rects)
    assert y0s[0] < y0s[-1]
    for r in rects:
        assert 0.0 <= r[0] < r[2] <= 1.0
        assert 0.0 <= r[1] < r[3] <= 1.0


@pytest.mark.unit
def test_chunk_bbox_tolerates_space_fused_tokens():
    """Markdown fuses words across breaks (``issueof``) that the text layer keeps
    split; word-overlap matching still locates the chunk where the legacy exact
    phrase search dropped it (#404)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "the issue of safety here today")
    pdf_bytes = doc.tobytes()
    doc.close()

    body = "the issueof safety here"  # 'issueof' fused, as markdown often renders it
    boundaries = [{"page": 1, "start_offset": 0, "end_offset": len(body)}]
    chunks = [(0, 0, len(body), 1, body)]

    results = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes, chunks=chunks, page_boundaries=boundaries, full_text=body
    )
    assert 0 in results  # located despite the fused token
    rects, _ = results[0]
    assert len(rects) >= 1


@pytest.mark.unit
def test_find_chunk_bbox_returns_single_union_box():
    """The legacy PNG-overlay path's ``_find_chunk_bbox`` collapses the per-line
    rects to one union box spanning all matched lines (#404)."""
    spacing = 30
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    lines = ["Alpha beta gamma", "delta epsilon zeta", "eta theta iota"]
    for i, ln in enumerate(lines):
        page.insert_text((50, 100 + i * spacing), ln)
    union = PDFHighlighter._find_chunk_bbox(page, " ".join(lines))
    doc.close()

    assert union is not None
    x0, y0, x1, y1 = union
    assert x0 < x1 and y0 < y1
    # Union spans all lines: taller than the first→last baseline gap.
    assert (y1 - y0) > spacing * (len(lines) - 1)
