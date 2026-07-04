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
  than classic OCR (typically 600–900s vs. the 120s default). Silently bumping the
  timeout would hide a mis-provisioned deployment, so the default is left as-is and
  the server logs a warning when `pipeline=vlm` is set with a timeout below 300s
  (checked on both touchpoints).
- **D4 — send `image_export_mode=placeholder` under `vlm`.** VLM output does not
  need embedded page images; `placeholder` keeps the response lean.
- **D5 — record the pipeline in metadata, keep `parsing_method`.** The result's
  `parsing_metadata` gains `docling_pipeline` (`standard`/`vlm`); `parsing_method`
  stays `"docling"` because the pipeline is a docling sub-detail, not a new backend.
- **D6 — do not validate preset names.** Presets are defined by the docling-serve
  instance and vary by deployment. An unknown preset produces a docling error that
  already maps to `ProcessorError`, so client-side validation would only add a
  brittle, deployment-specific allowlist.

## Consequences

- New env: `DOCLING_PIPELINE`, `DOCLING_VLM_PRESET`; `docling_pipeline` added to the
  validated enum (`{standard, vlm}`). Both flow through the config dual-surface
  (the Dynaconf image path and the `Settings`-dataclass OCR-backend path).
- **Fully backward compatible.** With `DOCLING_PIPELINE` unset (default `standard`)
  the request is identical to the ADR-031 client; nothing changes for existing
  deployments.
- **Operational note:** VLM needs a VLM-capable docling-serve (a preset backed by a
  real inference engine). The CI `docling` lane uses the CPU image with no VLM
  engine, so the VLM round-trip is covered by an **opt-in** integration test gated
  on `DOCLING_PIPELINE=vlm`; unit tests assert the request schema (that
  `pipeline=vlm` and the preset are actually sent) without needing an engine.
- Sync-only and the timeout implication carry over from ADR-031: a VLM run is a long
  synchronous convert, hence the larger recommended `DOCLING_TIMEOUT`. Async
  submit/poll remains future work.
