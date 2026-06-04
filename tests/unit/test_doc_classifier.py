"""Unit tests for the tier-0 document classifier.

Pins the routing decisions and the text-quality heuristic that drive which
extraction tier a PDF starts in:
  * a clean born-digital PDF (text, no full-page images) -> ``fast`` (tier 1);
  * a full-page-image scan -> ``ocr`` (tier 3), since handwriting/stamps aren't
    in any text layer;
  * the text-quality score distinguishes clean prose from mashed/space-less junk.
"""

import pymupdf
import pytest

from nextcloud_mcp_server.document_processors import classifier as clf

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


def _full_page_image_pdf(pages: int = 2) -> bytes:
    # A page whose entire area is a raster image -> looks scanned.
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 850))
    pix.clear_with(255)
    img = pix.tobytes("png")
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=img)
    data: bytes = doc.tobytes()
    doc.close()
    return data


# --- text-quality heuristic --------------------------------------------------


def test_text_quality_clean_prose_scores_high():
    assert clf._text_quality("the quick brown fox jumps over the lazy dog") > 0.8


def test_text_quality_mashed_tokens_scores_low():
    # space-less / mashed layer (the "Student 147" failure mode)
    mashed = "01322234567mobileoutstandingresilienceacademicachievementhurdles"
    assert clf._text_quality(mashed) < clf.MIN_TEXT_QUALITY


def test_text_quality_empty_is_zero():
    assert clf._text_quality("") == 0.0


# --- routing -----------------------------------------------------------------


def test_digital_pdf_routes_fast():
    c = clf.classify_pdf(_digital_pdf())
    assert c.recommended_tier == "fast"
    assert c.ocr_page_fraction == 0.0
    assert "image_heavy" not in c.flags
    assert c.mean_text_quality > 0.8


def test_full_page_image_routes_ocr():
    c = clf.classify_pdf(_full_page_image_pdf())
    assert c.recommended_tier == "ocr"
    assert c.ocr_page_fraction == 1.0
    assert "image_heavy" in c.flags


# --- sampling bounds large docs ----------------------------------------------


def test_large_doc_is_sampled():
    c = clf.classify_pdf(_digital_pdf(pages=120))
    assert c.page_count == 120
    assert c.sampled_pages <= clf.MAX_SAMPLED_PAGES


def test_sample_indices_includes_first_and_last_page():
    idx = clf._sample_indices(100)
    assert idx[0] == 0
    assert idx[-1] == 99  # last page must be sampled (scanned-tail case)
    assert len(idx) <= clf.MAX_SAMPLED_PAGES


# --- flag paths --------------------------------------------------------------


def _image_with_mashed_text_pdf(pages: int = 2) -> bytes:
    # Full-page image with a junk (mashed/space-less) text layer over it -- a
    # scan whose OCR'd text layer is unusable.
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 850))
    pix.clear_with(255)
    img = pix.tobytes("png")
    mashed = "01322234567mobileoutstandingresilienceacademicachievement " * 3
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=img)
        page.insert_text((50, 60), mashed)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def test_scanned_flag_when_no_text_layer():
    c = clf.classify_pdf(_full_page_image_pdf())
    assert c.total_chars == 0
    assert "scanned" in c.flags
    assert c.recommended_tier == "ocr"


def test_bad_text_layer_flag_on_image_with_junk_text():
    c = clf.classify_pdf(_image_with_mashed_text_pdf())
    assert c.total_chars > 0
    assert c.mean_text_quality < clf.MIN_TEXT_QUALITY
    assert "bad_text_layer" in c.flags
    assert c.recommended_tier == "ocr"
