# ADR-036: Native PPTX processor via python-pptx

## Status

Accepted — 2026-09-17 (revisits ADR-031 for `.pptx` specifically)

## Context

ADR-031 scoped `DoclingProcessor` (the `find_processor` auto-selection path) to
images only, and called office formats staying with `unstructured` an
"intentional non-goal". In practice that leaves a gap for any operator who
runs `ENABLE_DOCLING=true` without also standing up and enabling the separate
`unstructured` service: `is_parseable_document()` finds no processor for
`application/vnd.openxmlformats-officedocument.presentationml.presentation`,
and `nc_webdav_read_file` falls back to raw base64 for every PPTX — silently,
with `parse_status: "not_applicable"`.

An earlier version of this change routed PPTX/DOCX/XLSX through
`DoclingProcessor`, since docling-serve already parses OOXML correctly over
its existing `/v1/convert/file` endpoint. That widened `DoclingProcessor`'s
`supported_mime_types` but added no new capability of its own — it depends on
an operator having docling-serve deployed at all, which is no more guaranteed
than `unstructured`, and ties a presentation-specific parse to a service whose
reason for existing is OCR strength on scanned/handwritten content (ADR-031's
actual motivation), not office-document structure.

The in-flight native-processor work for `.doc`/`.docx`/`.xls`/`.xlsx`/`.msg`
(office/spreadsheet/msg processors) sets a better precedent for the
document's own shape: `.pptx` is OOXML — a zip of XML parts — so it does not
need an external service or a LibreOffice rendition at all.
[`python-pptx`](https://python-pptx.readthedocs.io/) reads it directly, in
process, as a pure-Python dependency.

## Decision

Add `PptxProcessor` (`document_processors/presentation.py`), a native reader
for `application/vnd.openxmlformats-officedocument.presentationml.presentation`
built on `python-pptx`:

- One markdown section per slide (`## Slide N`), assembled from each shape's
  text frame in shape order.
- A table shape becomes a markdown table (`shape.has_table`), so tabular
  slide content survives instead of being flattened.
- Speaker notes are appended to their slide's section (`**Notes:** ...`) when
  present.
- `slide_boundaries` metadata (`{slide, start_offset, end_offset}`) mirrors
  `page_boundaries`, so a chunk can still be attributed to
  the slide it came from despite a presentation having no page geometry to
  highlight.

Registered unconditionally at module load (`document_processors/__init__.py`),
like the built-in PDF tiers — no `ENABLE_*` flag, no external service, and no
LibreOffice availability check, since `python-pptx` is a plain dependency with
no binary to detect. Priority 15: above the optional `unstructured` processor
(10), which also claims this MIME type but flattens slide/table structure;
below Docling's images-only priority 20, where the two do not actually
compete today.

Legacy `.ppt` (OLE2) is explicitly out of scope: `python-pptx` cannot open the
binary container. It stays `unstructured`'s job (or a future
LibreOffice-rendition processor, if that gap needs closing).

This decision reverts the docling-routing approach outlined above; PDFs and
image auto-selection are unaffected by either.

## Consequences

- `nc_webdav_read_file` on a `.pptx` now returns extracted markdown instead of
  raw base64 unconditionally — no processor needs enabling.
- `python-pptx` becomes a core (non-dev) dependency.
- `DoclingProcessor.supported_mime_types` remains images-only, matching ADR-031
  as originally accepted; no auto-selection behavior changes there.
- `.docx`/`.xlsx` are not addressed here. If they follow the same "OOXML needs
  no external service" reasoning, they should get their own native readers
  (`python-docx`, `openpyxl`) rather than being folded into this processor or
  routed through docling.
