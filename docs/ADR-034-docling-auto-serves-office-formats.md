# ADR-034: Docling auto-serves OOXML office formats

## Status

Accepted — 2026-09-16 (revisits ADR-031)

## Context

ADR-031 scoped `DoclingProcessor` (the `find_processor` auto-selection path) to
images only, and explicitly called office formats staying with `unstructured` an
"intentional non-goal": docling was being introduced there purely for its OCR
strength on photographed/scanned/handwritten content, and `unstructured` already
covered PPTX/DOCX/XLSX.

In practice that leaves a gap for any operator who runs `ENABLE_DOCLING=true`
without also standing up and enabling the separate `unstructured` service
(`ENABLE_UNSTRUCTURED=true` + its own deployment) — a common shape, since
`unstructured` requires its own container and `docling-serve` is often deployed
already for the OCR/image touchpoints. For that operator, `is_parseable_document()`
finds no processor for `application/vnd.openxmlformats-officedocument.*`
mimetypes, and `nc_webdav_read_file` falls all the way back to raw base64 for
every PPTX/DOCX/XLSX — silently, with `parse_status: "not_applicable"` and no
indication that enabling one more env var would fix it.

docling already parses these formats correctly: `DoclingProcessor.process()`
already handles "PDFs/office formats" today (`_from_format_for_mime` returns
`None` for them, letting docling-serve infer the format from the filename
extension), it's just never auto-selected onto them. Verified directly against a
real internal PPTX (24 slides, mixed text/tables) via `docling-serve`'s
`POST /v1/convert/file` (`to_formats=md`): `status: "success"`, full text and
list structure reconstructed, no code changes needed on the docling-serve side.

## Decision

Add `DOCLING_OFFICE_TYPES` (`docling_serve.py`) — the three OOXML mimetypes
docling actually parses — and union it into `DoclingProcessor.supported_mime_types`
alongside the existing `DOCLING_IMAGE_TYPES`:

- `application/vnd.openxmlformats-officedocument.presentationml.presentation` (.pptx)
- `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (.docx)
- `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` (.xlsx)

Legacy binary formats (`.ppt`/`.doc`/`.xls`, OLE2 containers) are deliberately
**not** added — docling doesn't parse those, only `unstructured` does.

### Key design points

- **PDFs stay excluded.** This only widens the *office* carve-out from ADR-031;
  the PDF exclusion (`supported_mime_types` never includes `application/pdf`, so
  `_pdf_processor_for_tier` never matches docling) is unrelated and unchanged —
  docling still reaches PDFs only as the OCR-tier backend or a forced re-parse.
- **Priority order is unchanged, so its effect widens too.** `DoclingProcessor` is
  still registered at priority 20, above `unstructured`'s 10
  (`app.py::initialize_document_processors`). Previously that ordering only
  mattered for images; now, on a deployment running both processors, docling also
  wins PPTX/DOCX/XLSX routing over `unstructured`. This is intentional — docling
  is not a worse choice for OOXML than for images, and an operator who wants
  `unstructured` to keep handling office formats can still get that by not
  enabling docling, or by forcing `unstructured` via `processor_name`.
- **`auto` still never selects docling for anything without `DOCLING_API_URL`
  set.** No change to the registration guard from ADR-031.
- **No new env var.** This is a MIME-type-set change, not a new toggle —
  operators already running `ENABLE_DOCLING=true` get office support for free,
  matching what they'd reasonably expect from "docling parses office formats"
  (the module docstring's own claim, which was previously only true for the
  force-selected path).

## Consequences

- `nc_webdav_read_file` on a PPTX/DOCX/XLSX now returns extracted markdown
  instead of raw base64 whenever `ENABLE_DOCLING=true`, with no other
  configuration change.
- A deployment running both `ENABLE_DOCLING=true` and `ENABLE_UNSTRUCTURED=true`
  now routes office formats to docling instead of `unstructured` (previously only
  images were affected by this priority ordering).
- No new Python dependencies; docling-serve already accepts these formats over
  the existing `/v1/convert/file` client.
- Existing behavior for PDFs, and for deployments without `ENABLE_DOCLING`, is
  unchanged.
