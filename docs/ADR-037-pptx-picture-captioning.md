# ADR-037: PPTX picture captioning via docling-serve

## Status

Accepted — 2026-09-18 (extends ADR-036, reuses the docling-serve client added
in ADR-031/ADR-032).

## Context

ADR-036 gave `.pptx` a native `python-pptx` reader (`PptxProcessor`) that walks
each slide's shapes and extracts text frames and tables into markdown. It
deliberately does not touch docling: PPTX is OOXML, so no external service or
LibreOffice rendition is needed for its *text*.

That reader has a real gap, though: it only understands `has_table` and
`has_text_frame` shapes. A slide whose content is a picture — a pasted
screenshot, an exported diagram, a photo — produces no text at all for that
shape, silently. The slide's title and speaker notes still come through, but
the picture itself is invisible to the caller. In practice this is common:
decks routinely paste a diagram as an image rather than building it from
native PowerPoint shapes.

Two distinct things can be "a diagram on a slide", and only one of them is
addressable without a much larger change:

1. **A raster picture** (screenshot, exported PNG/JPEG, photo). This has an
   actual image part in the OOXML package — `python-pptx` exposes it as
   `shape.image.blob`. It can be sent anywhere an image can be sent, including
   the docling-serve instance the images-only `DoclingProcessor` (ADR-031)
   already talks to.
2. **A native vector diagram** (SmartArt, a group of freeform/connector
   shapes drawn directly in PowerPoint). This has no image part — it is
   drawing instructions, not pixels. The only way to "see" it is to rasterize
   the slide, which needs a rendering engine (LibreOffice headless, or
   similar). ADR-036 explicitly chose not to carry that dependency for the
   common text-only case, and this ADR does not revisit that choice. Vector
   diagrams remain out of scope here and stay undescribed; closing that gap
   is future work if it turns out to matter enough to justify an optional
   rendering dependency.

This ADR addresses only (1).

## Decision

`PptxProcessor` gains an optional captioning step for raster picture shapes,
reusing the existing docling-serve HTTP client (`convert_file()` in
`docling_serve.py`) rather than adding a new integration:

- **New settings** (all read directly off the `Settings` dataclass, the same
  surface `document_ocr_provider="docling"` already reads `docling_api_url`
  from — not the `app.py`-only processors-dict path `DoclingProcessor`/
  `ENABLE_DOCLING` are wired through, since `PptxProcessor` is registered at
  import time in `document_processors/__init__.py`, before that dict exists):
  - `PPTX_CAPTION_IMAGES` (bool, default `false`) — explicit opt-in.
  - `PPTX_CAPTION_MAX_IMAGES` (int, default `8`) — cap on docling round trips
    per file.
  - `PPTX_CAPTION_TIMEOUT_SECONDS` (float, default `15.0`) — per-picture
    request timeout, independent of `DOCLING_TIMEOUT`/
    `DOCUMENT_OCR_TIMEOUT_SECONDS` (other touchpoints, other latency
    profiles).
  - Captioning also needs `DOCLING_API_URL` (shared) and picks up
    `DOCLING_PIPELINE`/`DOCLING_VLM_PRESET`/`DOCLING_OCR_LANG` (shared,
    unchanged defaults).
- **Explicit opt-in beyond a bare `DOCLING_API_URL`**, deliberately — mirrors
  `DOCUMENT_OCR_PROVIDER` needing its own selection in ADR-031/032. A
  deployment that only wants docling for scanned-PDF OCR should not start
  captioning every picture in every presentation for free the moment a URL is
  configured.
- **Eligibility filter, before any HTTP call.** A picture shape is only sent
  to docling if: its `image.content_type` is one of `DoclingProcessor`'s own
  `DOCLING_IMAGE_TYPES` (so a vector paste embedded as EMF/WMF, which docling's
  image path cannot read either, costs nothing); and its native pixel size is
  at least `MIN_CAPTION_PICTURE_PX` (80px) on both axes. Sub-threshold pictures
  are treated as decorative — a logo, a bullet icon, a slide-master watermark —
  and skipped, so a capped budget is spent on content.
- **Extraction is restructured to two phases.** `_extract_deck` (still a
  worker-thread, CPU-only `python-pptx` walk) now returns structured
  `_SlideData` (blocks, notes, eligible pictures) instead of joined markdown.
  `process()` performs the (optionally capped, sequential) captioning calls —
  genuine network I/O — afterwards, on the event loop, then renders each
  slide's final markdown (text blocks + `*Image: <caption>*` lines + notes)
  and recomputes `slide_boundaries` against that final text.
- **A caption is appended, not interleaved.** Regardless of a picture's
  original shape position, its caption line is appended after the slide's
  text blocks (before `**Notes:**`), matching how notes are already appended
  rather than positionally interleaved. Simpler, and a slide's shape order is
  not meaningful reading order to begin with (placeholders, backgrounds, etc.
  do not reliably z-order the way prose would).
- **One picture's caption failing does not fail the deck.** `convert_file()`
  raising `ProcessorError` (HTTP error, timeout, bad docling status, empty
  output) for a given picture is caught, logged, and simply produces no
  caption line for that picture — mirrors how a missing/failed OCR backend
  degrades gracefully (`_ocr_note`) rather than failing the whole parse.
- **Honest degradation reporting.** `ProcessingResult.metadata` always
  includes `pptx_pictures_found` (count of eligible pictures, independent of
  whether captioning ran) and, only when captioning was attempted,
  `pptx_pictures_captioned`. A new `_pptx_caption_note()` in
  `document_parser.py` (parallel to `_markdown_note`/`_ocr_note`) turns these
  into a `parse_notes` entry on `nc_webdav_read_file`:
  - pictures found, captioning off/unconfigured → note that pictures exist
    and were not described.
  - pictures found, some not captioned (failure or cap) → note the count.
  - everything captioned, or no pictures → no note.

### Design decisions

- **D1 — read settings directly, don't depend on `DoclingProcessor`.**
  `PptxProcessor` is registered unconditionally at module import
  (`document_processors/__init__.py`), the same place `PyMuPDFProcessor`
  already reads its own settings from. `DoclingProcessor` itself is
  registered later, in `app.py`, from a separately-assembled
  `config["processors"]["docling"]` dict. Depending on that instance (or its
  dict) would tie `PptxProcessor`'s construction order to `app.py`'s for no
  real benefit; calling the module-level `convert_file()` function directly
  is exactly what `DoclingProcessor` itself does internally.
- **D2 — sequential, not concurrent, captioning.** `nc_webdav_read_file` is a
  synchronous interactive tool call either way; running captions concurrently
  would shave wall-clock time but pile concurrent load onto what is typically
  a CPU-only self-hosted docling-serve instance already IO-bound-single per
  ADR-031/032's own findings. `PPTX_CAPTION_MAX_IMAGES` bounds the total
  regardless.
- **D3 — no size-based downscale before upload.** A picture's raw blob is sent
  as-is. `MIN_CAPTION_PICTURE_PX` already filters out the pictures small
  enough that this would matter; genuinely large pictures are the useful case
  and downscaling them is not free (another decode/encode step per picture,
  in a hot path this ADR is already trying to keep bounded). Revisit if large
  decks with many big pictures turn out to be slow in practice.
- **D4 — caption text is used verbatim, not summarized further.** Whatever
  `convert_file()` returns (`md_content` preferred, `text_content` fallback —
  the same precedence `_document_text()` already uses) becomes the caption.
  Under the `standard` pipeline this is classic OCR (useful mainly when the
  picture is itself a text-heavy screenshot); under `vlm` it is a genuine
  natural-language description. No new text processing is added here.

## Consequences

- New env: `PPTX_CAPTION_IMAGES`, `PPTX_CAPTION_MAX_IMAGES`,
  `PPTX_CAPTION_TIMEOUT_SECONDS`. New `Settings` fields and `_DEFAULTS` entries
  (config.py), new `Validator`s for the two numeric ones.
- **Fully backward compatible.** `PPTX_CAPTION_IMAGES` defaults to `false`;
  with it unset, `PptxProcessor.process()` behaves exactly as before except
  for one addition to `ProcessingResult.metadata`
  (`pptx_pictures_found`, always present) — a caller that never asked for
  picture content sees at most one honest new fact about the deck, never a
  behavior change to the text itself.
- `_extract_deck`'s return type changed (`list[_SlideData]` instead of the
  final joined text) — internal to `presentation.py`; no public API changed.
- **Native vector diagrams (SmartArt, freeform shapes) remain undescribed.**
  This ADR does not add a rendering dependency. If that gap needs closing, it
  is a separate, larger decision (LibreOffice-based slide rasterization,
  optional and gated on its own `ENABLE_*` flag) left to a future ADR.
- Decorative pictures (logos, bullet icons, slide-master watermarks) are
  filtered by size before ever reaching docling, so enabling this on a
  branded template deck should not spend the whole `PPTX_CAPTION_MAX_IMAGES`
  budget on the corner logo repeated on every slide.
