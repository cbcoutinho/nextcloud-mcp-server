"""Tier-0 document classifier.

A cheap (<~1s), local pre-pass over a PDF that decides which extraction tier a
document should start in, BEFORE the expensive parse. It runs in *shadow mode*
first: emit the signals as metrics, change no routing, and gather per-tenant
data to tune the thresholds.

Signals (all cheap; no get_drawings, which is itself slow on the graphics-heavy
pages we'd want to flag -- the parse-time ``graphics_limit`` already makes those
safe, and the tier-1 quality gate catches unrecovered tables post-extraction):

  * text_layer_chars   -- extractable text per page
  * text_quality       -- is the text layer usable, or mashed/space-less junk?
                          (the "Student 147" lesson: a text layer can exist yet
                          be unusable, e.g. "01322234567mobile")
  * image_coverage     -- fraction of the page covered by raster images
                          (full-page image + poor text => scanned)

From these it picks a recommended starting tier:
  * ``ocr``  -- scanned / image-only / bad-text-layer (route to tier 3)
  * ``fast`` -- a usable digital text layer (route to tier 1)

``structured`` (tier 2 / docling) is intentionally not produced here -- that tier
is a separate service and is reached via the tier-1 quality gate, not tier-0.
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Page-sampling: classify at most this many pages on large docs (evenly spaced)
# so the pass stays bounded regardless of page count.
MAX_SAMPLED_PAGES = 24

# A page counts as "scanned-like" when a raster image covers most of it.
IMAGE_COVERAGE_SCANNED = 0.80
# Text-quality score below which the layer is treated as junk (mashed tokens).
MIN_TEXT_QUALITY = 0.45
# Fraction of sampled pages that must look scanned/bad for a doc->ocr verdict.
OCR_PAGE_FRACTION = 0.5

_WORD_RE = re.compile(r"\S+")


@dataclass
class PageSignals:
    page_no: int
    char_count: int
    image_coverage: float  # 0..1 of page area covered by images
    text_quality: float  # 0..1; low = mashed/space-less/garbage layer
    needs_ocr: bool  # scanned or unusable text layer


@dataclass
class DocClassification:
    page_count: int
    sampled_pages: int
    total_chars: int
    mean_text_quality: float
    ocr_page_fraction: float  # fraction of sampled pages flagged needs_ocr
    recommended_tier: str  # "fast" | "ocr"
    flags: set[str] = field(
        default_factory=set
    )  # scanned | bad_text_layer | image_heavy
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
    # Caps at 1.0 from 12% whitespace (conservative; clean prose runs 15-20%),
    # mean token ~4-6 chars, ~no overlong tokens.
    ws_score = min(whitespace_ratio / 0.12, 1.0)
    len_score = (
        1.0 if mean_token_len <= 10 else max(0.0, 1.0 - (mean_token_len - 10) / 15)
    )
    overlong_score = max(0.0, 1.0 - overlong_frac * 5)
    return round(ws_score * len_score * overlong_score, 3)


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


def classify_pdf(content: bytes) -> DocClassification:
    """Classify a PDF from its bytes.

    May raise (e.g. ``pymupdf`` errors) if the bytes can't be opened as a PDF;
    callers run it in a guarded context (shadow mode swallows failures) so a
    bad file never breaks indexing.
    """
    import pymupdf  # noqa: PLC0415 -- keep the heavy import lazy / off module load

    doc = pymupdf.open("pdf", content)
    try:
        page_count = doc.page_count
        indices = _sample_indices(page_count)
        pages: list[PageSignals] = []
        for n in indices:
            page = doc.load_page(n)
            text = page.get_text("text")
            quality = _text_quality(text)
            page_area = abs(page.rect.width * page.rect.height) or 1.0
            img_area = 0.0
            for img in page.get_images(full=True):
                for rect in page.get_image_rects(img[0]):
                    img_area += abs(rect.width * rect.height)
            # Approximate: an image placed multiple times (tiled backgrounds) is
            # double-counted, so img_area can exceed page_area -- the min() caps
            # coverage at 1.0, which is all the scanned/digital split needs.
            coverage = min(img_area / page_area, 1.0)
            # A page that is mostly a raster image is a scan/photo: its content
            # (handwriting, stamps, figure text) is not fully in any text layer,
            # so OCR is needed to capture it -- regardless of whether a partial
            # text layer is present. Text quality/char-count are kept as
            # diagnostic signals (flags + tuning metrics), not the trigger,
            # because OCR only helps when there is an image to read.
            needs_ocr = coverage >= IMAGE_COVERAGE_SCANNED
            pages.append(
                PageSignals(n, len(text), round(coverage, 3), quality, needs_ocr)
            )
    finally:
        doc.close()

    sampled = len(pages)
    total_chars = sum(p.char_count for p in pages)
    mean_quality = (
        round(sum(p.text_quality for p in pages) / sampled, 3) if sampled else 0.0
    )
    ocr_frac = (sum(p.needs_ocr for p in pages) / sampled) if sampled else 0.0

    # Flags are diagnostic signals, intentionally independent of the routing
    # verdict: image_heavy fires if ANY page is image-heavy, while the OCR route
    # needs a FRACTION of pages (OCR_PAGE_FRACTION). So a mostly-digital doc with
    # one full-page photo is flagged image_heavy yet still routes "fast" -- the
    # flag_total{image_heavy} count is expected to exceed classified{ocr}.
    flags: set[str] = set()
    if any(p.image_coverage >= IMAGE_COVERAGE_SCANNED for p in pages):
        flags.add("image_heavy")
    if (
        ocr_frac >= OCR_PAGE_FRACTION
        and total_chars
        and mean_quality < MIN_TEXT_QUALITY
    ):
        flags.add("bad_text_layer")
    if ocr_frac >= OCR_PAGE_FRACTION and total_chars == 0:
        flags.add("scanned")

    recommended = "ocr" if ocr_frac >= OCR_PAGE_FRACTION else "fast"

    return DocClassification(
        page_count=page_count,
        sampled_pages=sampled,
        total_chars=total_chars,
        mean_text_quality=mean_quality,
        ocr_page_fraction=round(ocr_frac, 3),
        recommended_tier=recommended,
        flags=flags,
        pages=pages,
    )
