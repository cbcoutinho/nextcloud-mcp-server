"""Unit tests for PDFHighlighter.compute_chunk_bboxes_batch (Deck #76).

Replaces the legacy `highlight_chunks_batch`-+-base64 pipeline that inflated
Qdrant payloads with per-chunk PNG screenshots. The new path returns
normalized bounding boxes only.
"""

from __future__ import annotations

import logging

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


# --- Per-page word cache (compute_chunk_bboxes_batch reuses each page's words
# across all of its chunks -> O(pages), not O(chunks)) -----------------------


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
    pdf_bytes = _make_pdf(
        [
            "The quick brown fox jumps over the lazy dog near the river bank "
            "while the sun sets slowly behind the distant rolling green hills.",
        ]
    )
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
    """A word-less page extracts once and reuses the empty result across chunks."""
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


def _many_page_fixture(n_pages: int) -> tuple[bytes, list[dict], str, list[tuple]]:
    """An n-page PDF with one chunk per page, for window-boundary testing."""
    pages = [
        f"Page {i} content about storage synchronisation and indexing behaviour."
        for i in range(1, n_pages + 1)
    ]
    pdf_bytes = _make_pdf(pages)
    boundaries, full_text = _page_boundaries(pages)
    chunks = [
        (i, b["start_offset"], b["end_offset"], b["page"], pages[i])
        for i, b in enumerate(boundaries)
    ]
    return pdf_bytes, boundaries, full_text, chunks


@pytest.mark.unit
@pytest.mark.parametrize("window", [1, 2, 3, 5, 12, 13, 100])
def test_windowed_bboxes_identical_to_unwindowed(window):
    """Windowing bounds MuPDF page retention; it must NOT change results.

    MuPDF keeps parsed page objects alive for the lifetime of the Document, so
    a single open across a long document accumulates ~0.154 MB/page (+617 MB
    measured on a real 4003-page file, which OOMKilled the ingest workers).
    Reopening every `page_window` pages fixes that, but only if the output is
    identical -- otherwise it silently moves highlight rectangles.

    12 pages exercises windows that divide the count exactly (1, 2, 3, 12), one
    that leaves a remainder (5), and ones larger than the document (13, 100).
    Off-by-one at the window boundary is the obvious failure mode.
    """
    pdf_bytes, boundaries, full_text, chunks = _many_page_fixture(12)

    kwargs = {
        "pdf_bytes": pdf_bytes,
        "chunks": chunks,
        "page_boundaries": boundaries,
        "full_text": full_text,
    }
    unwindowed = PDFHighlighter.compute_chunk_bboxes_batch(**kwargs, page_window=0)
    windowed = PDFHighlighter.compute_chunk_bboxes_batch(**kwargs, page_window=window)

    assert windowed == unwindowed
    # Guard against the degenerate pass where both are empty and "identical".
    assert len(unwindowed) == 12


@pytest.mark.unit
def test_windowing_actually_reopens_the_document(mocker):
    """The document must be REOPENED per window, not just produce equal output.

    Parity alone cannot catch a regression that stops windowing: results would
    still match while memory silently regressed. Assert the open count instead,
    which is the property that bounds retention.
    """
    pdf_bytes, boundaries, full_text, chunks = _many_page_fixture(12)
    spy = mocker.spy(pymupdf, "open")

    PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=full_text,
        page_window=3,
    )

    # 12 pages carrying chunks / window of 3 == 4 opens.
    assert spy.call_count == 4


@pytest.mark.unit
def test_window_zero_opens_document_once(mocker):
    """page_window=0 is the documented escape hatch: one open for the document."""
    pdf_bytes, boundaries, full_text, chunks = _many_page_fixture(12)
    spy = mocker.spy(pymupdf, "open")

    PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=chunks,
        page_boundaries=boundaries,
        full_text=full_text,
        page_window=0,
    )

    assert spy.call_count == 1


@pytest.mark.unit
def test_windowing_only_opens_pages_that_carry_chunks(mocker):
    """Windows count pages WITH chunks, so a sparse document stays cheap.

    A 12-page document with chunks on 2 pages must not cost 12/window opens.
    """
    pdf_bytes, boundaries, full_text, _ = _many_page_fixture(12)
    sparse = [
        (0, boundaries[0]["start_offset"], boundaries[0]["end_offset"], 1, "x"),
        (1, boundaries[11]["start_offset"], boundaries[11]["end_offset"], 12, "x"),
    ]
    spy = mocker.spy(pymupdf, "open")

    PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=sparse,
        page_boundaries=boundaries,
        full_text=full_text,
        page_window=1,
    )

    # Two pages carry chunks, window of 1 -> 2 opens, not 12.
    assert spy.call_count == 2


@pytest.mark.unit
@pytest.mark.parametrize("window", [0, 100])
def test_no_resolvable_pages_returns_empty_without_error(window, caplog):
    """Chunks that match no page must return {} cleanly, at any window setting.

    With windowing disabled the window size is derived from the number of pages
    carrying chunks; if that is zero, computing the window before checking would
    build range(0, 0, 0) -> "arg 3 must not be zero", surfacing as a bogus
    "Error computing chunk bboxes" instead of an honest empty result.
    """
    pdf_bytes, boundaries, full_text, _ = _many_page_fixture(3)
    # Offsets far past every page boundary -> find_chunk_page finds nothing.
    unresolvable = [(0, 10_000, 10_100, None, "nowhere")]

    caplog.set_level(
        logging.ERROR, logger="nextcloud_mcp_server.search.pdf_highlighter"
    )
    results = PDFHighlighter.compute_chunk_bboxes_batch(
        pdf_bytes=pdf_bytes,
        chunks=unresolvable,
        page_boundaries=boundaries,
        full_text=full_text,
        page_window=window,
    )

    assert results == {}
    assert "Error computing chunk bboxes" not in caplog.text
