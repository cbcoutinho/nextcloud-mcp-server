# ADR-038: Native DOCX/XLSX readers

## Status

Accepted — 2026-09-18 (follows ADR-036 for `.docx`/`.xlsx`, reuses the
captioning path of ADR-037)

## Context

ADR-036 gave `.pptx` a native `python-pptx` reader and noted that `.docx` and
`.xlsx` should follow the same "OOXML needs no external service" reasoning with
their own readers, rather than being folded into `PptxProcessor` or routed
through docling. Until then, without the optional `unstructured` service,
`nc_webdav_read_file` finds no processor for either type and returns raw base64.

PR #1265 proposed a different route: `.docx` via a LibreOffice PDF rendition
parsed at the structured tier. That needs LibreOffice in the image and a
subprocess per document. The native readers below need neither. #1265's
`.doc`/`.xls`/`.msg` support is untouched by this ADR. Those formats have no
pure-Python OOXML reader, and remain its concern.

## Decision

### `.docx` — `DocxProcessor` (`document_processors/word.py`, python-docx)

- The body is walked in document order (`Document.iter_inner_content()`):
  - paragraphs become text
  - `Title`/`Heading N` styles become `#`-headings
  - `List*` styles, or direct numbering (`w:numPr`), become `- ` items
  - tables become markdown tables
- python-docx repeats a merged cell in every grid position it spans, so table
  columns stay aligned (the failure #1265 measured for mammoth, which drops the
  merged cell and shifts the row, does not arise).
- Inline and floating pictures (`a:blip/@r:embed` in a paragraph) go through
  the shared ADR-037 eligibility filter and captioner. A caption is placed
  **right after the paragraph that holds the picture**. A slide's caption is
  appended to the slide, but prose has a reading order worth keeping.
- Skipped: headers/footers (logos, page numbers), pictures inside table cells,
  and legacy `.doc`.
- Registered unconditionally at priority 15, like `PptxProcessor`, above the
  optional `unstructured` processor (10).
- No page or section boundaries: a `.docx` has no page geometry until rendered,
  and nothing consumes a heading-span map today.

## Consequences

- `nc_webdav_read_file` on a `.docx` returns markdown out of the box.
- `python-docx` becomes a core dependency (pure Python; `lxml` was already
  present).
- One `PictureCaptioner`, built from `OFFICE_CAPTION_*`, is shared by every
  OOXML reader.
