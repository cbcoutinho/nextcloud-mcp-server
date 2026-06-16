"""Unit tests for the tier-0 document classifier.

Pins the routing decisions and the text-quality heuristic that drive which
extraction tier a PDF starts in:
  * a clean born-digital PDF (text, no full-page images) -> ``fast`` (tier 1);
  * a full-page-image scan with no usable text layer -> ``ocr`` (tier 3);
  * routing is on TEXT signals only -- image coverage feeds the ``image_heavy``
    diagnostic flag but does not route (a mostly-raster page with a clean text
    layer stays ``fast``);
  * the text-quality score distinguishes clean prose from mashed/space-less junk.
"""

import pymupdf
import pytest

from nextcloud_mcp_server.document_processors import classifier as clf
from tests.fixtures.glyph_corruption import GLYPH_CORRUPT_TEXT

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


def _glyph_corrupt_pdf(pages: int = 2) -> bytes:
    # A born-digital PDF whose text layer carries the glyph-leak control chars,
    # for the classify_pdf (diagnostic) path. pymupdf round-trips the C0 controls.
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 60), GLYPH_CORRUPT_TEXT)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _full_page_image_pdf(pages: int = 2) -> bytes:
    # A page whose entire area is a raster image -> looks scanned.
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 850))
    pix.clear_with(255)
    img = pix.tobytes("png")
    del pix  # Pixmap holds native memory; release it before the loop
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
    assert clf._text_quality("") == pytest.approx(0.0)


# --- routing -----------------------------------------------------------------


def test_digital_pdf_routes_fast():
    c = clf.classify_pdf(_digital_pdf())
    assert c.recommended_tier == "fast"
    assert c.ocr_page_fraction == pytest.approx(0.0)
    assert "image_heavy" not in c.flags
    assert c.mean_text_quality > 0.8


def test_full_page_image_routes_ocr():
    c = clf.classify_pdf(_full_page_image_pdf())
    assert c.recommended_tier == "ocr"
    assert c.ocr_page_fraction == pytest.approx(1.0)
    assert "image_heavy" in c.flags
    assert "scanned" in c.flags  # no text layer at all


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
    del pix  # Pixmap holds native memory; release it before the loop
    mashed = "01322234567mobileoutstandingresilienceacademicachievement " * 3
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=img)
        page.insert_text((50, 60), mashed)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _image_with_clean_text_pdf(pages: int = 2) -> bytes:
    # Full-page image with a CLEAN embedded text layer -- a scan carrying a good
    # OCR layer, or a figure-heavy digital page. Image-heavy but usable text.
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 850))
    pix.clear_with(255)
    img = pix.tobytes("png")
    del pix  # Pixmap holds native memory; release it before the loop
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=img)
        page.insert_text((50, 60), "Hello world this is clean text. " * 8)
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


def _mostly_text_one_image_pdf() -> bytes:
    # 3 digital text pages + 1 full-page-image page: one image-heavy page, but
    # ocr_frac = 1/4 < OCR_PAGE_FRACTION, so the doc routes fast.
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 600, 850))
    pix.clear_with(255)
    img = pix.tobytes("png")
    del pix  # Pixmap holds native memory; release it before the loop
    for _ in range(3):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 60), "Hello world this is clean text. " * 8)
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=img)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def test_image_heavy_flag_without_ocr_routing():
    # The documented asymmetry operators rely on: a mostly-digital doc with one
    # full-page image carries the image_heavy flag yet still routes fast.
    c = clf.classify_pdf(_mostly_text_one_image_pdf())
    assert "image_heavy" in c.flags
    assert c.recommended_tier == "fast"
    assert c.ocr_page_fraction < clf.OCR_PAGE_FRACTION


# --- classify_from_text (hot-path, derived from tier-1 extraction) -----------


def test_classify_from_text_clean_routes_fast():
    txt = "the quick brown fox jumps over the lazy dog " * 3
    c = clf.classify_from_text(
        txt, [{"page": 1, "start_offset": 0, "end_offset": len(txt)}]
    )
    assert c.recommended_tier == "fast"
    assert c.mean_text_quality > 0.8
    assert c.flags == set()


def test_classify_from_text_empty_routes_ocr():
    c = clf.classify_from_text("", [{"page": 1, "start_offset": 0, "end_offset": 0}])
    assert c.recommended_tier == "ocr"
    assert "scanned" in c.flags  # unified with classify_pdf's flag name
    assert c.total_chars == 0


def test_classify_from_text_no_pages_routes_fast():
    # An empty/corrupt PDF (no page boundaries) is not OCR evidence -> "fast",
    # so the recorded classification metric isn't a misleading "ocr".
    c = clf.classify_from_text("", [])
    assert c.recommended_tier == "fast"
    assert c.ocr_page_fraction == pytest.approx(0.0)
    assert c.flags == set()


def test_classify_from_text_junk_layer_flags_bad_text_layer():
    # Each short segment (<MIN_PAGE_CHARS) sets needs_ocr -> high ocr_frac, and
    # total_chars>0 with mean_quality<MIN_TEXT_QUALITY (no-whitespace junk scores
    # 0.0) -> bad_text_layer (gated on ocr_frac, matching classify_pdf).
    text = "x1y2zx1y2z"
    c = clf.classify_from_text(
        text,
        [
            {"page": 1, "start_offset": 0, "end_offset": 5},
            {"page": 2, "start_offset": 5, "end_offset": 10},
        ],
    )
    assert c.recommended_tier == "ocr"
    assert c.total_chars > 0
    assert "bad_text_layer" in c.flags
    assert "scanned" not in c.flags  # has text, just junk -> not the empty case


# --- quality + scan escalation triggers (Deck #207) --------------------------

_JUNK = (
    "ST. TRINIAN'SSCHOOLSTUDENT RECORDFILE struggledsignificantlywith "
    "learningdifficulties demonstrateda positiveattitude academictasks"
)
_CLEAN = "the quick brown fox jumps over the lazy dog and then runs away home"


def _two_page(text_a: str, text_b: str):
    na = len(text_a)
    return text_a + text_b, [
        {"page": 1, "start_offset": 0, "end_offset": na},
        {"page": 2, "start_offset": na, "end_offset": na + len(text_b)},
    ]


def test_classify_from_text_low_quality_routes_ocr():
    full, bounds = _two_page(_JUNK, _JUNK)
    c = clf.classify_from_text(full, bounds)
    assert c.recommended_tier == "ocr"
    assert "bad_text_layer" in c.flags


def test_quality_floor_override_disables_trigger():
    # min_text_quality=0.0 => quality never trips; text present + not scanned => fast
    full, bounds = _two_page(_JUNK, _JUNK)
    c = clf.classify_from_text(full, bounds, min_text_quality=0.0)
    assert c.recommended_tier == "fast"


def test_image_heavy_clean_text_stays_fast():
    # Image coverage is diagnostic, not routing: fully-raster pages whose text
    # layer is already clean (a scan carrying a good OCR layer, or a figure-heavy
    # digital page) carry the image_heavy flag but stay on the fast tier --
    # re-OCR adds nothing. This was the ~45% over-escalation on OHR-Bench.
    full, bounds = _two_page(_CLEAN, _CLEAN)
    c = clf.classify_from_text(full, bounds, image_coverage=[1.0, 1.0])
    assert c.recommended_tier == "fast"
    assert "image_heavy" in c.flags
    assert all(p.needs_ocr is False for p in c.pages)


def test_classify_pdf_image_heavy_clean_text_stays_fast():
    # classify_pdf symmetry with test_image_heavy_clean_text_stays_fast: full-page
    # raster images WITH a clean embedded text layer are image_heavy but route
    # fast -- coverage is diagnostic, not routing, on the classify_pdf path too.
    c = clf.classify_pdf(_image_with_clean_text_pdf())
    assert c.recommended_tier == "fast"
    assert "image_heavy" in c.flags
    assert c.mean_text_quality >= clf.MIN_TEXT_QUALITY


def test_scan_signal_ignored_when_coverage_low():
    full, bounds = _two_page(_CLEAN, _CLEAN)
    c = clf.classify_from_text(full, bounds, image_coverage=[0.1, 0.0])
    assert c.recommended_tier == "fast"


def test_page_fraction_override():
    # exactly one of two pages is junk -> ocr_frac 0.5
    full, bounds = _two_page(_CLEAN, _JUNK)
    assert (
        clf.classify_from_text(full, bounds, page_fraction=0.5).recommended_tier
        == "ocr"
    )
    assert (
        clf.classify_from_text(full, bounds, page_fraction=0.6).recommended_tier
        == "fast"
    )


def test_image_coverage_per_page():
    scan = clf.image_coverage_per_page(_full_page_image_pdf(pages=2))
    assert len(scan) == 2 and all(c >= 0.8 for c in scan)
    digital = clf.image_coverage_per_page(_digital_pdf(pages=2))
    assert len(digital) == 2 and all(c < 0.1 for c in digital)


def test_scan_coverage_shorter_than_pages_aligns_without_crash():
    # image_coverage shorter than the boundaries (the MAX_SAMPLED_PAGES cap):
    # the single entry aligns to page 0; later pages get no coverage entry. With
    # clean text everywhere, routing is on text only, so nothing escalates.
    n = len(_CLEAN)
    full = _CLEAN * 3
    bounds = [
        {"page": 1, "start_offset": 0, "end_offset": n},
        {"page": 2, "start_offset": n, "end_offset": 2 * n},
        {"page": 3, "start_offset": 2 * n, "end_offset": 3 * n},
    ]
    c = clf.classify_from_text(full, bounds, image_coverage=[1.0])
    assert c.pages[0].image_coverage == pytest.approx(1.0)  # entry aligned
    assert c.pages[1].image_coverage == pytest.approx(0.0)  # no entry -> 0
    assert all(p.needs_ocr is False for p in c.pages)  # coverage no longer routes
    assert "image_heavy" in c.flags  # but page 0 still flags image_heavy
    assert c.recommended_tier == "fast"


# --- glyph-corruption signal (broken /ToUnicode -> structured escalation) -----

# A uniform glyph/Caesar offset turns clean prose into alphabetic-but-wrong tokens
# (normal spacing + token length => HIGH text_quality) while digits/punctuation map
# to C0 control bytes. The control-char ratio is the only signal that catches this;
# _text_quality scores it ~1.0. Shared with the registry tiering tests.
_GLYPH_CORRUPT = GLYPH_CORRUPT_TEXT


def test_control_char_ratio_clean_is_zero():
    assert clf._control_char_ratio("the quick brown fox") == pytest.approx(0.0)
    # legitimate whitespace controls (tab/newline/CR/form-feed/vtab) don't count
    assert clf._control_char_ratio("a\tb\nc\r\nd\f\ve") == pytest.approx(0.0)


def test_control_char_ratio_detects_glyph_leak():
    assert clf._control_char_ratio(_GLYPH_CORRUPT) > clf.GLYPH_CORRUPTION_RATIO


def test_clean_text_not_flagged_corrupt():
    txt = "the quick brown fox jumps over the lazy dog " * 3
    c = clf.classify_from_text(
        txt, [{"page": 1, "start_offset": 0, "end_offset": len(txt)}]
    )
    assert "corrupt_glyphs" not in c.flags
    assert c.mean_control_ratio == pytest.approx(0.0)
    assert c.recommended_tier == "fast"


def test_glyph_corrupt_routes_structured_not_ocr():
    full = _GLYPH_CORRUPT
    c = clf.classify_from_text(
        full, [{"page": 1, "start_offset": 0, "end_offset": len(full)}]
    )
    assert c.recommended_tier == "structured"
    assert "corrupt_glyphs" in c.flags
    # The point: it is NOT a low-quality signal -- the cipher scores high, so only
    # the control-char ratio diverts it (to structured, the free pymupdf re-parse).
    assert c.mean_text_quality >= clf.MIN_TEXT_QUALITY
    assert c.mean_control_ratio > clf.GLYPH_CORRUPTION_RATIO


def test_glyph_corruption_ratio_override_disables_trigger():
    full = _GLYPH_CORRUPT
    bounds = [{"page": 1, "start_offset": 0, "end_offset": len(full)}]
    # A threshold of 1.0 can never be exceeded => not treated as corrupt => the
    # other (high-quality) signals win => fast.
    c = clf.classify_from_text(full, bounds, glyph_corruption_ratio=1.0)
    assert c.recommended_tier == "fast"
    assert "corrupt_glyphs" not in c.flags


def test_glyph_corruption_ratio_zero_disables_trigger():
    full = _GLYPH_CORRUPT
    bounds = [{"page": 1, "start_offset": 0, "end_offset": len(full)}]
    # 0 disables the signal (rather than firing on any single control byte).
    c = clf.classify_from_text(full, bounds, glyph_corruption_ratio=0.0)
    assert c.recommended_tier == "fast"
    assert "corrupt_glyphs" not in c.flags


def test_empty_doc_routes_ocr_not_structured():
    # Precedence: a scanned/empty doc (no text layer) has no control chars to leak,
    # so it must stay an OCR case, never structured.
    c = clf.classify_from_text("", [{"page": 1, "start_offset": 0, "end_offset": 0}])
    assert c.recommended_tier == "ocr"
    assert "corrupt_glyphs" not in c.flags


def test_glyph_corrupt_takes_precedence_over_junk_text_layer():
    # A layer that is BOTH glyph-corrupt (high control ratio) AND junk-quality
    # (mashed, no whitespace -> low text_quality): both flags fire, but
    # glyph-corrupt wins the route (structured, not ocr) -- the structured
    # re-extract is the cheaper correct fix, and re-classification catches any
    # residual junk afterwards.
    text = "WKHTXLFNEURZQIRAMXPSV\x0f\x10\x11\x0f\x10" * 3
    c = clf.classify_from_text(
        text, [{"page": 1, "start_offset": 0, "end_offset": len(text)}]
    )
    assert c.recommended_tier == "structured"
    assert "corrupt_glyphs" in c.flags
    assert "bad_text_layer" in c.flags


def test_classify_pdf_glyph_corrupt_routes_structured():
    # Symmetry with the classify_from_text routing on the standalone/diagnostic
    # classify_pdf path (which re-opens the PDF and samples pages).
    c = clf.classify_pdf(_glyph_corrupt_pdf())
    assert c.recommended_tier == "structured"
    assert "corrupt_glyphs" in c.flags
    assert c.mean_control_ratio > clf.GLYPH_CORRUPTION_RATIO
    assert c.mean_text_quality >= clf.MIN_TEXT_QUALITY  # control signal, not quality
