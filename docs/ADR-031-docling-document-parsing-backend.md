# ADR-031: Docling document-parsing backend (docling-serve)

## Status

Accepted — 2026-07-01

## Context

Files read via WebDAV (`nc_webdav_read_file`) are returned as base64, decoded
UTF-8 text, or — when document processing is enabled — text extracted by the
pluggable processor registry (`document_processors/`). The registered HTTP OCR
option, `unstructured`, does poorly on photographed, scanned and especially
**handwritten** documents.

[docling](https://github.com/docling-project/docling) has substantially stronger
OCR for exactly that content and is deployable as a standalone HTTP service,
[docling-serve](https://github.com/docling-project/docling-serve). We want to use
external docling-serve instances (per `DOCLING_API_URL`) alongside `unstructured`
without adding ML dependencies to the MCP server image and without regressing the
existing PDF pipeline.

Relevant existing architecture (reused, not replaced):

- The registry has **two independent routing paths**: `find_processor()` picks by
  **priority** for images/non-PDF; `_process_pdf()` runs a tiered pipeline
  (`fast → structured → ocr`) for PDFs, with the `ocr` tier gated on
  `document_ocr_enabled`.
- `classifier.classify_from_text()` already **auto-detects a missing/unusable PDF
  text layer** (scanned → recommend `ocr`), so born-digital PDFs stay on the cheap
  local tiers and only genuine scans reach OCR.
- The `ocr` tier (`OcrProcessor`) already has **pluggable backends** (`_OcrBackend`:
  gateway, Mistral) selected by `document_ocr_provider`, returning
  `(text, page_boundaries, block_spans)`.
- `registry.process(processor_name=...)` already supports a **forced processor**
  that bypasses tiering and MIME auto-selection.

## Decision

Add docling at three touchpoints, sharing one docling-serve HTTP client
(`document_processors/docling_serve.py`). `POST /v1/convert/file` (synchronous
multipart) is the client; `GET /health` is the probe.

1. **Images → docling (automatic).** A standalone `DoclingProcessor`
   (`supported_mime_types` = images only, `tier="fast"`) is registered at
   **priority 20** (above `unstructured`'s 10) in `initialize_document_processors()`
   when `ENABLE_DOCLING=true` **and** `DOCLING_API_URL` is set. Images therefore
   always route to docling when enabled. Gated only by `ENABLE_DOCLING` +
   `DOCLING_API_URL` (the image/`find_processor` path has no OCR gate).

2. **Scanned PDFs → docling (automatic, opt-in).** A `_DoclingServeBackend`
   (`_OcrBackend`) is added to `ocr.py` and selected by
   `DOCUMENT_OCR_PROVIDER=docling`. With `DOCUMENT_OCR_ENABLED=true`, PDFs the
   classifier flags as scanned/no-text-layer escalate to the OCR tier and are
   transcribed by docling. It requests `to_formats=json,md` and reconstructs
   per-page `page_boundaries` by grouping `DoclingDocument.texts[].prov[].page_no`,
   falling back to a single whole-text page when provenance is absent.

3. ~~**Text-layer PDFs → docling (on demand).**~~ **Superseded in 0.151.0 (Deck
   #894).** This touchpoint was a `force_processor` argument on
   `nc_webdav_read_file`, threaded to the registry's forced path so
   `force_processor="docling"` re-parsed any file with docling. It was removed:
   it asked the caller (an LLM) to name an extraction engine it had no basis to
   choose, and the forced path bypassed both tiering and the pre-parse size
   guard. The two needs it served are now met without it — a text layer that
   misses tables is handled locally by `parse_document="markdown"` (the
   `structured`/pymupdf4llm tier), and a genuinely scanned PDF reaches docling
   through touchpoint 2, where docling belongs as an OCR *provider*.
   `DoclingProcessor` still handles PDFs internally (deriving `from_formats` from
   the MIME type), but nothing routes one to it any more.

### Key design points

- **Images-only `supported_mime_types` + `tier="fast"`.** Keeps docling out of the
  automatic PDF tier selection (`_pdf_processor_for_tier` matches on tier **and**
  `supports("application/pdf")`), so enabling docling never reroutes every PDF
  through it or collides with the existing `ocr` tier. PDFs reach docling only via
  touchpoint 2 (OCR backend) or touchpoint 3 (explicit force).
- **`auto` never selects docling.** docling needs an explicit self-hosted URL, so
  it is chosen only by `DOCUMENT_OCR_PROVIDER=docling`.
- **Registration guarded on `DOCLING_API_URL`.** A bare `ENABLE_DOCLING` without a
  URL registers nothing, so it can't shadow other image processors with a dead
  endpoint (mirrors the custom-processor guard).
- **`block_spans` left empty.** docling's block bboxes don't follow the gateway's
  normalized `[0,1]` contract, so highlighting falls back to pymupdf (same as the
  Mistral backend).
- **Office formats stay with `unstructured`** — intentional non-goal; docling is
  scoped to images/scans/handwriting here.
- **Synchronous only.** docling-serve's sync convert has an observed ~2 min
  practical ceiling (from our testing — not a hard, server-enforced contract), so
  a larger client `DOCLING_TIMEOUT` (e.g. 300s for slow CPU OCR) is harmless: it
  just lets a genuinely slow conversion finish rather than capping it below the
  server's own limit. Async submit/poll (`/v1/convert/file/async`) is future work
  for very large scans.

## Consequences

- New env: `ENABLE_DOCLING`, `DOCLING_API_URL`, `DOCLING_TIMEOUT`,
  `DOCLING_OCR_LANG`, `DOCLING_DO_OCR`; `docling` added to the
  `document_ocr_provider` enum. A `docling` docker-compose profile runs
  docling-serve for testing.
- OCR language codes are engine-dependent (docling default EasyOCR: `en,de`;
  Tesseract-backed: `eng,deu`) — operator-tunable, documented, not hard-coded.
- No new Python dependencies (HTTP via the existing `httpx`).
- Existing behavior is unchanged when `ENABLE_DOCLING` is unset and
  `DOCUMENT_OCR_PROVIDER` ≠ `docling`.
