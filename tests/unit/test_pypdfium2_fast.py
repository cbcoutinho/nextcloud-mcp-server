"""Unit tests for the tier-1 pypdfium2 fast PDF extractor."""

import functools

import pymupdf
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.document_processors.pypdfium2_fast import (
    Pypdfium2FastProcessor,
    _extract,
)

pytestmark = pytest.mark.unit


def _digital_pdf(
    pages: int = 3, body: str = "Hello world this is clean text. "
) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 60), body * 8)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def test_processor_identity():
    p = Pypdfium2FastProcessor()
    assert p.name == "pypdfium2_fast"
    assert p.tier == "fast"
    assert "application/pdf" in p.supported_mime_types


async def test_extract_text_and_exact_page_boundaries():
    p = Pypdfium2FastProcessor()
    result = await p.process(_digital_pdf(pages=3), "application/pdf", filename="t.pdf")

    assert result.success is True
    assert "Hello world" in result.text
    assert result.metadata["page_count"] == 3

    boundaries = result.metadata["page_boundaries"]
    assert len(boundaries) == 3
    assert boundaries[0]["start_offset"] == 0
    # Offsets must index exactly into the returned text (pdf_highlighter contract).
    assert boundaries[-1]["end_offset"] == len(result.text)
    for prev, nxt in zip(boundaries, boundaries[1:]):
        assert prev["end_offset"] == nxt["start_offset"]


async def test_malformed_pdf_returns_success_false():
    p = Pypdfium2FastProcessor()
    result = await p.process(b"not a pdf at all", "application/pdf", filename="bad.pdf")

    assert result.success is False
    assert result.text == ""
    assert result.metadata["parse_failed_reason"] == "error"


async def test_health_check():
    assert await Pypdfium2FastProcessor().health_check() is True


# Page-windowed extraction (see _extract). PDFium retains parsed page objects for
# the document's lifetime, so the extractor re-opens the document every N pages to
# bound peak RSS. Windowing must not change a single byte of the output, and the
# page_boundaries offsets must keep indexing exactly into the joined text --
# search/pdf_highlighter and PageAwareChunker both depend on that contract.
@functools.cache
def _numbered_pdf(pages: int) -> bytes:
    """A PDF whose page i says ``Page i``, so a page out of order shows."""
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=595, height=842).insert_text((50, 60), f"Page {i} body")
    data: bytes = doc.tobytes()
    doc.close()
    return data


@functools.cache  # shared across examples: callers must not mutate the result
def _unwindowed(pages: int) -> tuple[str, dict]:
    return _extract(_numbered_pdf(pages), 0)


@st.composite
def windowed_documents(draw) -> tuple[int, int]:
    """A page count and a window: 0 (off), dividing it or not, or larger."""
    pages = draw(st.integers(1, 9))
    return pages, draw(st.integers(0, pages + 3))


@given(windowed_documents())
@example((7, 3))  # a window that does not divide the page count
@example((7, 100))
def test_page_window_output_is_identical(case):
    """Windowing must not change a byte of the output, and each page's
    boundaries must index exactly that page's text in order."""
    pages, window = case
    text, meta = _extract(_numbered_pdf(pages), window)

    assert (text, meta) == _unwindowed(pages)
    boundaries = meta["page_boundaries"]
    assert meta["page_count"] == len(boundaries) == pages
    assert boundaries[0]["start_offset"] == 0
    assert boundaries[-1]["end_offset"] == len(text)
    for prev, nxt in zip(boundaries, boundaries[1:]):
        assert prev["end_offset"] == nxt["start_offset"]
    for i, b in enumerate(boundaries):
        assert text[b["start_offset"] : b["end_offset"]].startswith(f"Page {i} ")


def test_page_window_handles_empty_document():
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    content: bytes = doc.tobytes()
    doc.close()

    text, meta = _extract(content, 100)

    assert meta["page_count"] == 1
    assert meta["page_boundaries"][0]["start_offset"] == 0
    assert meta["page_boundaries"][-1]["end_offset"] == len(text)


async def test_process_source_parses_from_a_path_not_bytes(tmp_path):
    """The path form must produce the same result as the bytes form.

    PdfDocument(path) uses FPDF_LoadDocument, which reads incrementally, while
    PdfDocument(bytes) uses FPDF_LoadMemDocument64 and pins the buffer for the
    document's lifetime.
    """
    from nextcloud_mcp_server.document_source import SpooledDocumentSource

    content = _digital_pdf(pages=4)
    spool = tmp_path / "doc.pdf"
    spool.write_bytes(content)
    source = SpooledDocumentSource(spool, "application/pdf", "doc.pdf")
    processor = Pypdfium2FastProcessor()

    from_path = await processor.process_source(source)
    from_bytes = await processor.process(content, "application/pdf", "doc.pdf")

    assert from_path.success is True
    assert from_path.text == from_bytes.text
    assert (
        from_path.metadata["page_boundaries"] == from_bytes.metadata["page_boundaries"]
    )
    assert from_path.metadata["file_size"] == len(content)
