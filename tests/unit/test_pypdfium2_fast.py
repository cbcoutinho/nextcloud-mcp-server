"""Unit tests for the tier-1 pypdfium2 fast PDF extractor."""

import pymupdf
import pytest

from nextcloud_mcp_server.document_processors.pypdfium2_fast import (
    Pypdfium2FastProcessor,
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
