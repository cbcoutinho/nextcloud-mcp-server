# ADR-032: Docling VLM pipeline (client-selected)

## Status

Accepted — 2026-07-04 (extends ADR-031)

## Context

ADR-031 added docling-serve as an OCR-strong parsing backend at three touchpoints
(images, scanned PDFs, on-demand force), all sharing one HTTP client,
`document_processors/docling_serve.py`. That client calls
`POST /v1/convert/file` and — critically — **never sends a `pipeline` field**, so
docling-serve always runs its default **`standard`** pipeline: classic OCR
(EasyOCR / RapidOCR / Tesseract).

docling-serve also ships a **VLM** pipeline (`pipeline=vlm`) that transcribes with
a vision-language model — often markedly better on handwriting, messy scans and
complex layouts. A VLM run is selected by `pipeline=vlm` plus an optional
`vlm_pipeline_preset` naming a server-defined preset (e.g. `glm_ocr` backed by a
local Ollama). A real deployment runs docling-serve **VLM-only** with such a
preset, but because the MCP client never sends `pipeline=vlm`, every request fell
through to classic OCR and the VLM presets were never exercised. docling-serve has
**no server-side "default pipeline" switch**, so the fix must be **client-side**.

The request field names were verified live against docling-serve v1.26.0's
`GET /openapi.json`: `/v1/convert/file` really accepts the form fields `pipeline`,
`vlm_pipeline_preset` and `image_export_mode`. (A wrong field name would be
silently ignored and fall back to `standard` — i.e. exactly the bug.)

## Decision

Add two opt-in operator settings, shared by **both** docling touchpoints that call
`convert_file()` (the image `DoclingProcessor` and the scanned-PDF
`_DoclingServeBackend`):

- **`DOCLING_PIPELINE`** ∈ {`standard`, `vlm`}, default `standard`.
- **`DOCLING_VLM_PRESET`** `str | None`, default `None` (→ docling-serve picks its
  own default preset).

`convert_file()` gains `pipeline` / `vlm_pipeline_preset` keyword arguments. When
`pipeline == "vlm"` it sends `pipeline=vlm`, the preset (if set) and
`image_export_mode=placeholder`, and **omits** `do_ocr`/`ocr_lang`. Otherwise it
emits exactly the pre-VLM request — the default path is byte-for-byte unchanged.

### Design decisions

- **D1 — client-selected, two settings.** No server default exists, so the client
  chooses. One pair of settings feeds both touchpoints (both call `convert_file()`).
- **D2 — omit `do_ocr`/`ocr_lang` under `vlm`.** They belong to the classic OCR
  pipeline and are inert for VLM; sending them would be misleading.
- **D3 — do not override the timeout; warn instead.** VLM inference is far slower
  than classic OCR (a quick A30 benchmark of Granite-Vision on docling's default
  inline-transformers engine ran ~90–200s/page, vs. a few s/page for classic OCR).
  Silently bumping a timeout would hide a mis-provisioned deployment, so defaults are
  left as-is and the server warns. **Which timeout to raise depends on the path** (see
  "Interactive reads vs. async ingest" below): raise `DOCUMENT_OCR_TIMEOUT_SECONDS`
  for the async ingest/OCR path (where a long budget is appropriate); do **not**
  inflate `DOCLING_TIMEOUT`, which only lengthens how long the interactive
  `nc_webdav_read_file` tool blocks.
- **D4 — send `image_export_mode=placeholder` under `vlm`.** VLM output does not
  need embedded page images; `placeholder` keeps the response lean.
- **D5 — record the pipeline in metadata, keep `parsing_method`.** The result's
  `parsing_metadata` gains `docling_pipeline` (`standard`/`vlm`); `parsing_method`
  stays `"docling"` because the pipeline is a docling sub-detail, not a new backend.
- **D6 — do not validate preset names.** Presets are defined by the docling-serve
  instance and vary by deployment. An unknown preset produces a docling error that
  already maps to `ProcessorError`, so client-side validation would only add a
  brittle, deployment-specific allowlist.

## Interactive reads vs. async ingest

VLM widens the blast radius of a subtlety that predates this ADR: the two docling
touchpoints run in very different execution contexts, and each has its **own,
independent** timeout.

- **Async ingest (bulk, out-of-band).** When `DOCUMENT_OCR_PROVIDER=docling`, scanned
  PDFs are transcribed by the OCR tier on the **background ingest pipeline**
  (`vector/scanner.py` → the `ingest-ocr` procrastinate queue → `vector/processor.py`,
  running under `mcp_role=worker`), and the result is written to Qdrant for semantic
  search. This path is governed by **`DOCUMENT_OCR_TIMEOUT_SECONDS`** and never blocks
  a tool call. **This is the right home for VLM at any volume** — raise
  `DOCUMENT_OCR_TIMEOUT_SECONDS` here freely.
- **Interactive read (on-demand, synchronous).** `nc_webdav_read_file` parses inline
  (`await parse_document(...)`), so an image (auto-routed to `DoclingProcessor`) or a
  `force_processor="docling"` PDF makes a **blocking** POST to docling-serve for up to
  **`DOCLING_TIMEOUT`**. Under VLM (~90–200s/page) this call blocks for minutes.

**Explicit callout (the operative constraint):** raising `DOCLING_TIMEOUT` for VLM
directly increases how long `nc_webdav_read_file` blocks, and MCP clients typically
enforce a much shorter per-tool timeout (~30–60s). The client will usually kill the
call before docling responds, so the user sees a client-side timeout rather than the
tool's base64 fallback. Interactive VLM (multi-minute) and short MCP client timeouts
are fundamentally incompatible; VLM is best paired with the async/ingest path, not
interactive reads. **Images are interactive-only** — the ingest scanner is PDF-only,
so images are never indexed; interactive VLM image reads inherently block.

**Mitigation — `DOCUMENT_READ_TIMEOUT_SECONDS` (opt-in).** A new setting caps the
synchronous parse *inside* `nc_webdav_read_file` (via `anyio.fail_after`), independent
of `DOCLING_TIMEOUT` and of the worker path. When the cap trips, the tool returns
base64 **quickly** instead of hanging until the client times out. Default is `None`
(disabled) to avoid silently regressing existing ADR-031 interactive scanned-PDF reads
(and the interactive-VLM user who deliberately accepts long calls); operators who care
about client-timeout safety set it to a client-friendly bound (e.g. 45–60s). It never
applies to the async ingest/worker path, which keeps its full `DOCUMENT_OCR_TIMEOUT_SECONDS`
budget.

## Consequences

- New env: `DOCLING_PIPELINE`, `DOCLING_VLM_PRESET`; `docling_pipeline` added to the
  validated enum (`{standard, vlm}`). Both flow through the config dual-surface
  (the Dynaconf image path and the `Settings`-dataclass OCR-backend path). Plus
  `DOCUMENT_READ_TIMEOUT_SECONDS` (opt-in interactive read cap, default off).
- **Fully backward compatible.** With `DOCLING_PIPELINE` unset (default `standard`)
  the request is identical to the ADR-031 client; nothing changes for existing
  deployments. `DOCUMENT_READ_TIMEOUT_SECONDS` unset = no behavioral change.
- **Operational note:** VLM needs a VLM-capable docling-serve (a preset backed by a
  real inference engine). The CI `docling` lane uses the CPU image with no VLM
  engine, so the VLM round-trip is covered by an **opt-in** integration test gated
  on `DOCLING_PIPELINE=vlm`; unit tests assert the request schema (that
  `pipeline=vlm` and the preset are actually sent) without needing an engine.
- Sync-only carries over from ADR-031: a VLM run is a long synchronous convert. For
  bulk VLM raise `DOCUMENT_OCR_TIMEOUT_SECONDS` on the ingest path; keep
  `DOCLING_TIMEOUT` client-friendly and use `DOCUMENT_READ_TIMEOUT_SECONDS` to bound
  interactive reads. Async submit/poll for the interactive path remains future work.
