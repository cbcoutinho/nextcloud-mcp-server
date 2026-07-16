"""Tier-0 document classifier.

Decides which extraction tier a PDF should escalate to, from cheap signals:
  * text_quality -- is the text layer usable, or mashed/space-less junk? (the
    "Student 147" lesson: a text layer can exist yet be unusable, e.g.
    "01322234567mobile")
  * no text layer -- the strongest OCR signal available from text alone.

Routing is on the TEXT signals only: a page escalates to OCR when its text is
near-empty or junk-quality. ``image_coverage`` is computed but is a DIAGNOSTIC
signal (the ``image_heavy`` flag), NOT a routing trigger: a mostly-raster page
whose embedded text is already usable (a scan carrying a clean OCR layer, or a
digital page dominated by a figure) gains nothing from re-OCR, so escalating it
to the paid OCR tier was wasteful -- on OHR-Bench the coverage trigger drove
~45% of escalations. The trade-off: image-only content on an otherwise-clean
page (handwriting, stamps, figure text) is no longer force-routed to OCR.

Two entry points:
  * ``classify_from_text(text, page_boundaries, ...)`` -- the HOT PATH. Routes on
    text-quality + near-empty pages derived from the tier-1 extraction (~no
    cost). When OCR + scan-detection are enabled the registry also passes
    per-page ``image_coverage`` (from ``image_coverage_per_page``) for the
    ``image_heavy`` diagnostic flag; that image pass is the only added cost and
    only OCR-opted-in tenants pay it. Thresholds come from per-tenant settings.
  * ``classify_pdf(content)`` -- a standalone/diagnostic pass that re-opens the
    PDF and does image-coverage analysis inline. Off the hot path.

Recommended tier:
  * ``ocr``  -- scanned / no-usable-text-layer (route to tier 3, when enabled)
  * ``structured`` -- a text layer that is present but glyph-corrupt (the fast
    extractor leaked raw glyph codes; high C0-control-char ratio). A different
    in-cluster extractor (the pymupdf ``structured`` tier) recovers it -- no OCR.
  * ``fast`` -- a usable digital text layer (stay on tier 1)
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Page-sampling: classify at most this many pages on large docs (evenly spaced)
# so the pass stays bounded regardless of page count.
MAX_SAMPLED_PAGES = 24

# Raster-image coverage above which a page raises the DIAGNOSTIC ``image_heavy``
# flag. This is observability only -- it does NOT route to OCR (see module
# docstring); routing is on the text signals alone.
IMAGE_HEAVY_THRESHOLD = 0.80
# Text-quality score below which the layer is treated as junk (mashed tokens).
# Kept in sync with the DOCUMENT_OCR_MIN_TEXT_QUALITY setting default so the
# module/diagnostic default matches production (the registry always passes the
# setting).
MIN_TEXT_QUALITY = 0.5
# Fraction of sampled pages that must look scanned/bad for a doc->ocr verdict.
OCR_PAGE_FRACTION = 0.5
# A page with fewer extracted chars than this has effectively no text layer.
MIN_PAGE_CHARS = 16
# Doc-level control-character ratio above which the text layer is treated as
# glyph-corrupt and routed to the ``structured`` (pymupdf) tier, which re-extracts
# such PDFs correctly. Kept in sync with the DOCUMENT_GLYPH_CORRUPTION_RATIO
# setting default (the registry passes the per-tenant value). See
# ``_control_char_ratio``.
GLYPH_CORRUPTION_RATIO = 0.02

_WORD_RE = re.compile(r"\S+")

# Whitespace control characters that legitimately appear in extracted text
# (tab / newline / carriage-return / form-feed / vertical-tab). Every OTHER C0
# control char is a corruption signal -- see ``_control_char_ratio``.
_TEXT_WHITESPACE_CONTROLS = frozenset("\t\n\r\f\v")


@dataclass
class PageSignals:
    page_no: int
    char_count: int
    image_coverage: float  # 0..1 of page area covered by images
    text_quality: float  # 0..1; low = mashed/space-less/garbage layer
    needs_ocr: bool  # scanned or unusable text layer
    control_ratio: float = 0.0  # 0..1; high = corrupt/glyph-leak text layer


@dataclass
class DocClassification:
    page_count: int
    sampled_pages: int
    total_chars: int
    mean_text_quality: float
    ocr_page_fraction: float  # fraction of sampled pages flagged needs_ocr
    # Classifier vocabulary (coarse): "fast" | "structured" | "ocr". "ocr" means
    # "needs OCR" — the registry resolves it to the OCR tier via
    # next_available_tier (the backend/model are configured, not tier-split).
    recommended_tier: str
    mean_control_ratio: float = 0.0  # doc-level C0-control-char ratio (glyph-leak)
    flags: set[str] = field(
        default_factory=set
    )  # scanned | bad_text_layer | image_heavy | corrupt_glyphs
    pages: list[PageSignals] = field(default_factory=list)


def _text_quality(text: str) -> float:
    """Score a text layer's usability in ``[0, 1]`` (1 = clean prose).

    Penalises the two hallmarks of a junk/OCR-mangled layer: too little
    whitespace (words mashed together) and very long tokens. Empty text scores
    0 -- "no usable layer".
    """
    if not text:
        return 0.0
    tokens = _WORD_RE.findall(text)
    if not tokens:
        return 0.0
    whitespace_ratio = sum(c.isspace() for c in text) / len(text)
    mean_token_len = sum(len(t) for t in tokens) / len(tokens)
    overlong_frac = sum(len(t) > 20 for t in tokens) / len(tokens)
    # Word-merging (dropped inter-word spaces) is the dominant junk-text-layer
    # failure mode on scanned forms -- the older whitespace/overlong(>20) terms
    # miss it, because the merges are 10-20 chars and a few dropped spaces still
    # leave whitespace above the 0.12 cap. Clean prose keeps <~3% of tokens above
    # 12 chars; merged/OCR-mangled layers push it past 10%. (Measured: the junk
    # Student-147 scan scores ~0.20 here vs >=0.9 for clean digital docs.)
    long_frac = sum(len(t) > 12 for t in tokens) / len(tokens)
    # Caps at 1.0 from 12% whitespace (conservative; clean prose runs 15-20%),
    # mean token ~4-6 chars, ~no overlong tokens.
    ws_score = min(whitespace_ratio / 0.12, 1.0)
    len_score = (
        1.0 if mean_token_len <= 10 else max(0.0, 1.0 - (mean_token_len - 10) / 15)
    )
    overlong_score = max(0.0, 1.0 - overlong_frac * 5)
    merge_score = max(0.0, 1.0 - max(0.0, long_frac - 0.03) / 0.12)
    return round(ws_score * len_score * overlong_score * merge_score, 3)


def _control_char_ratio(text: str) -> float:
    """Fraction of C0 control characters (excluding whitespace controls) in ``text``.

    Near 0 for clean text in ANY script; elevated when the extractor leaked raw
    glyph codes instead of Unicode -- the broken-/ToUnicode failure mode where a
    subset font's character codes are returned uniformly offset (e.g. "WKH" for
    "THE"). This is the language-agnostic counterpart to :func:`_text_quality`: a
    uniform glyph/Caesar offset preserves whitespace and token lengths (so every
    ``_text_quality`` factor scores it ~1.0), but it litters the text with C0
    controls -- digits/punctuation map to bytes below 0x20 -- which clean prose
    never contains. Unlike a dictionary or stop-word probe it makes no assumption
    about the document's language.
    """
    if not text:
        return 0.0
    bad = sum(1 for c in text if ord(c) < 0x20 and c not in _TEXT_WHITESPACE_CONTROLS)
    return bad / len(text)


def _sample_indices(page_count: int) -> list[int]:
    if page_count <= MAX_SAMPLED_PAGES:
        return list(range(page_count))
    # Evenly spaced sample that always includes the first AND last page, so a
    # scanned tail on an otherwise-digital doc isn't missed. Rounding collisions
    # just yield a slightly smaller (still bounded) sample.
    last = page_count - 1
    return sorted(
        {round(i * last / (MAX_SAMPLED_PAGES - 1)) for i in range(MAX_SAMPLED_PAGES)}
    )


def _page_image_coverage(page: Any) -> float:
    """Fraction of a pymupdf page covered by raster images, in ``[0, 1]``.

    Approximate: an image placed multiple times (tiled backgrounds) is
    double-counted, so the raw area can exceed the page -- the min() caps
    coverage at 1.0, which is all the scanned/digital split needs.
    """
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    img_area = 0.0
    for img in page.get_images(full=True):
        for rect in page.get_image_rects(img[0]):
            img_area += abs(rect.width * rect.height)
    return min(img_area / page_area, 1.0)


def _route_from_signals(
    *,
    total_chars: int,
    ocr_frac: float,
    mean_quality: float,
    control_ratio: float,
    image_heavy: bool,
    page_fraction: float,
    min_text_quality: float,
    glyph_corruption_ratio: float,
) -> tuple[set[str], str]:
    """Shared flag-set + recommended-tier decision for both classifier paths.

    Routing precedence (cheapest correct fix first):
      1. scanned / no text layer (``ocr_frac >= fraction`` AND ``total_chars == 0``)
         -> ``"ocr"``
      2. glyph-corrupt text layer (``control_ratio > glyph_corruption_ratio``)
         -> ``"structured"``. pypdfium2 leaked glyph codes; the pymupdf
         ``structured`` tier re-extracts these correctly, so no paid OCR is
         needed. The registry re-classifies the structured output, so a doc that
         is ALSO partly scanned can still escalate to OCR from there.
      3. junk/mashed text layer (``ocr_frac >= fraction``) -> ``"ocr"``
      4. otherwise -> ``"fast"``

    Flags are diagnostic and independent of the verdict (e.g. ``image_heavy``
    fires on ANY image-heavy page; the OCR route needs a page FRACTION).
    """
    # glyph_corruption_ratio <= 0 disables the signal (a ratio of 0 would otherwise
    # fire on any single C0 control byte). total_chars > 0 also guarantees this
    # never overlaps the scanned branch (total_chars == 0), so a doc is never both
    # glyph-corrupt and "scanned".
    glyph_corrupt = (
        glyph_corruption_ratio > 0
        and total_chars > 0
        and control_ratio > glyph_corruption_ratio
    )

    flags: set[str] = set()
    if ocr_frac >= page_fraction and total_chars == 0:
        flags.add("scanned")
    elif ocr_frac >= page_fraction and mean_quality < min_text_quality:
        flags.add("bad_text_layer")
    if glyph_corrupt:
        flags.add("corrupt_glyphs")
    if image_heavy:
        flags.add("image_heavy")

    if ocr_frac >= page_fraction and total_chars == 0:
        recommended = "ocr"
    elif glyph_corrupt:
        recommended = "structured"
    elif ocr_frac >= page_fraction:
        recommended = "ocr"
    else:
        recommended = "fast"
    return flags, recommended


def classify_pdf(content: bytes) -> DocClassification:
    """Classify a PDF from its bytes.

    May raise (e.g. ``pymupdf`` errors) if the bytes can't be opened as a PDF;
    callers run it in a guarded context (shadow mode swallows failures) so a
    bad file never breaks indexing.
    """
    import pymupdf  # noqa: PLC0415 -- keep the heavy import lazy / off module load

    from nextcloud_mcp_server.document_processors._native_locks import (  # noqa: PLC0415
        pymupdf_serialized,
    )

    # MuPDF is not thread-safe; serialize against concurrent bbox/parse threads.
    with pymupdf_serialized(), pymupdf.open("pdf", content) as doc:
        page_count = doc.page_count
        indices = _sample_indices(page_count)
        pages: list[PageSignals] = []
        for n in indices:
            page = doc.load_page(n)
            text = page.get_text("text")
            quality = _text_quality(text)
            coverage = _page_image_coverage(page)
            # OCR-worthy on TEXT signals only (kept in sync with
            # classify_from_text): a junk/low-quality text layer (the
            # word-merging case) or an effectively empty one. Image coverage is
            # deliberately NOT a routing trigger -- a mostly-raster page whose
            # embedded text is already usable (a scan with a clean OCR layer, or
            # a digital page dominated by a figure) gains nothing from re-OCR, so
            # routing it to the paid OCR tier was wasteful. High coverage still
            # raises the diagnostic image_heavy flag below.
            needs_ocr = quality < MIN_TEXT_QUALITY or len(text.strip()) < MIN_PAGE_CHARS
            pages.append(
                PageSignals(
                    n,
                    len(text),
                    round(coverage, 3),
                    quality,
                    needs_ocr,
                    round(_control_char_ratio(text), 4),
                )
            )

    sampled = len(pages)
    total_chars = sum(p.char_count for p in pages)
    mean_quality = (
        round(sum(p.text_quality for p in pages) / sampled, 3) if sampled else 0.0
    )
    ocr_frac = (sum(p.needs_ocr for p in pages) / sampled) if sampled else 0.0
    # Char-weighted doc-level control-char ratio (p.control_ratio * char_count is
    # the per-page bad-char count). The glyph-leak signal -- see _control_char_ratio.
    # NOTE: this is over the <=MAX_SAMPLED_PAGES sample, so unlike classify_from_text
    # (which scans the whole full_text) this diagnostic path can under-detect
    # corruption concentrated outside the sampled pages. Acceptable here: the hot
    # path is classify_from_text; this standalone pass is for diagnostics.
    control_ratio = (
        sum(p.control_ratio * p.char_count for p in pages) / total_chars
        if total_chars
        else 0.0
    )

    flags, recommended = _route_from_signals(
        total_chars=total_chars,
        ocr_frac=ocr_frac,
        mean_quality=mean_quality,
        control_ratio=control_ratio,
        image_heavy=any(p.image_coverage >= IMAGE_HEAVY_THRESHOLD for p in pages),
        page_fraction=OCR_PAGE_FRACTION,
        min_text_quality=MIN_TEXT_QUALITY,
        glyph_corruption_ratio=GLYPH_CORRUPTION_RATIO,
    )

    return DocClassification(
        page_count=page_count,
        sampled_pages=sampled,
        total_chars=total_chars,
        mean_text_quality=mean_quality,
        ocr_page_fraction=round(ocr_frac, 3),
        recommended_tier=recommended,
        mean_control_ratio=round(control_ratio, 4),
        flags=flags,
        pages=pages,
    )


def image_coverage_per_page(content: bytes) -> list[float]:
    """Raster-image coverage in ``[0, 1]`` for every page (document order).

    Feeds the ``image_heavy`` DIAGNOSTIC flag only (coverage no longer routes --
    see module docstring). Re-opens the PDF, so the registry calls it only when
    OCR + scan detection are enabled (the cost is borne by OCR-opted-in tenants).
    Returned list is aligned by index with the leading page boundaries.

    Bounded to the first ``MAX_SAMPLED_PAGES`` pages -- the image pass is the
    costly part, so a 200-page scan isn't fully rasterised on the hot path.
    """
    import pymupdf  # noqa: PLC0415 -- keep the heavy import lazy

    from nextcloud_mcp_server.document_processors._native_locks import (  # noqa: PLC0415
        pymupdf_serialized,
    )

    cov: list[float] = []
    # MuPDF is not thread-safe. This runs on the event loop during escalation, so
    # the lock also serializes it against concurrent bbox/parse worker threads.
    with pymupdf_serialized(), pymupdf.open("pdf", content) as doc:
        for n in range(min(doc.page_count, MAX_SAMPLED_PAGES)):
            cov.append(_page_image_coverage(doc.load_page(n)))
    return cov


def classify_from_text(
    full_text: str,
    page_boundaries: list[dict[str, Any]],
    *,
    min_text_quality: float = MIN_TEXT_QUALITY,
    min_page_chars: int = MIN_PAGE_CHARS,
    page_fraction: float = OCR_PAGE_FRACTION,
    glyph_corruption_ratio: float = GLYPH_CORRUPTION_RATIO,
    image_coverage: list[float] | None = None,
) -> DocClassification:
    """Classify from text already extracted by tier-1 -- no PDF re-open by default.

    The hot-path classifier. A page is OCR-worthy when its text is near-empty
    (``< min_page_chars``) or its text-quality is junk (``< min_text_quality`` --
    the word-merging signal). The doc recommends ``ocr`` once
    ``ocr_frac >= page_fraction``. A doc whose text layer is present but
    glyph-corrupt (doc-level C0-control-char ratio ``> glyph_corruption_ratio``,
    the broken-/ToUnicode failure mode) instead recommends ``structured`` -- the
    pymupdf tier re-extracts it correctly, no OCR needed. Thresholds are passed in
    by the registry from per-tenant settings. ``image_coverage`` (when supplied)
    only feeds the ``image_heavy`` diagnostic flag -- it does NOT route (see
    module docstring).

    ``page_boundaries`` are ``{page, start_offset, end_offset}`` indexing into
    ``full_text``; ``image_coverage[i]`` (if given) aligns with the i-th boundary.

    Note: the ``image_heavy`` flag is only set when ``image_coverage`` is
    supplied, so for tenants with scan detection off that flag is always zero.
    Routing is unaffected either way -- it is on the text signals alone.
    """
    # image_coverage is expected to be one entry per page, capped at
    # MAX_SAMPLED_PAGES (see image_coverage_per_page). Any other length means the
    # 1:1 page alignment drifted (e.g. the extractor reordered/skipped pages) --
    # log it so a contract break surfaces rather than silently misattributing
    # coverage to the wrong pages.
    if image_coverage is not None:
        expected = min(len(page_boundaries), MAX_SAMPLED_PAGES)
        if len(image_coverage) != expected:
            logger.debug(
                "image_coverage length %s != expected %s for %s boundaries; "
                "scan signal may be misaligned",
                len(image_coverage),
                expected,
                len(page_boundaries),
            )

    pages: list[PageSignals] = []
    for idx, b in enumerate(page_boundaries):
        seg = full_text[b["start_offset"] : b["end_offset"]]
        quality = _text_quality(seg)
        # image_coverage is one entry per PDF page, aligned 1:1 with the
        # boundaries; the length guard is belt-and-suspenders against a mismatch.
        cov = (
            image_coverage[idx]
            if image_coverage is not None and idx < len(image_coverage)
            else 0.0
        )
        # Routing is on TEXT signals only: a near-empty layer or a junk/
        # space-mangled one. Image coverage (``cov``) is intentionally not a
        # routing trigger -- a mostly-raster page with an already-usable text
        # layer does not benefit from re-OCR, so escalating it to the paid OCR
        # tier was wasteful (on OHR-Bench this drove ~45% of escalations: clean
        # digital figure-pages and scans that already carry a good OCR layer).
        # ``cov`` still feeds the diagnostic ``image_heavy`` flag below.
        needs_ocr = len(seg.strip()) < min_page_chars or quality < min_text_quality
        pages.append(
            PageSignals(
                b["page"],
                len(seg),
                round(cov, 3),
                quality,
                needs_ocr,
                round(_control_char_ratio(seg), 4),
            )
        )

    sampled = len(pages)
    total_chars = sum(p.char_count for p in pages)
    mean_quality = (
        round(sum(p.text_quality for p in pages) / sampled, 3) if sampled else 0.0
    )
    # No pages (empty/corrupt PDF) => no OCR evidence => "fast" (the registry's
    # page_count guard also skips escalation; defaulting to 0.0 keeps the
    # recorded classification metric accurate rather than a misleading "ocr").
    ocr_frac = (sum(p.needs_ocr for p in pages) / sampled) if sampled else 0.0
    # Doc-level control-char ratio -- the glyph-leak signal that routes to the
    # structured tier. Computed over the WHOLE full_text (all pages), unlike
    # classify_pdf which char-weights the up-to-MAX_SAMPLED_PAGES sample; the two
    # are therefore not numerically identical for a >24-page doc with corruption
    # concentrated outside the sample. full_text is used here because it is exactly
    # the text that gets chunked + indexed and is robust to boundary edge cases.
    control_ratio = _control_char_ratio(full_text)

    flags, recommended = _route_from_signals(
        total_chars=total_chars,
        ocr_frac=ocr_frac,
        mean_quality=mean_quality,
        control_ratio=control_ratio,
        image_heavy=any(p.image_coverage >= IMAGE_HEAVY_THRESHOLD for p in pages),
        page_fraction=page_fraction,
        min_text_quality=min_text_quality,
        glyph_corruption_ratio=glyph_corruption_ratio,
    )

    return DocClassification(
        page_count=len(page_boundaries),
        sampled_pages=sampled,
        total_chars=total_chars,
        mean_text_quality=mean_quality,
        ocr_page_fraction=round(ocr_frac, 3),
        recommended_tier=recommended,
        mean_control_ratio=round(control_ratio, 4),
        flags=flags,
        pages=pages,
    )
