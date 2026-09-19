# ADR-039: Legacy and ODF office formats via a shared Collabora Online service

## Status

Accepted — 2026-09-19 (supersedes the LibreOffice-in-image approach of PR #1265;
builds on the native OOXML readers of ADR-036/038)

## Context

ADR-036 and ADR-038 gave `.pptx`, `.docx` and `.xlsx` native, in-process
readers. That leaves the office formats that have no usable pure-Python reader:

- legacy binary Office: `.doc`, `.xls`, `.ppt` (OLE2 containers)
- OpenDocument: `.odt`, `.ods`, `.odp`, which Nextcloud Office creates by default

LibreOffice opens all of them. PR #1265 ran it as a `soffice` subprocess
inside the MCP server image. That approach has costs:

- **Image and pod weight.** Writer and Calc add hundreds of MB to an image that
  serves interactive MCP requests, and every API and worker pod carries them.
- **Memory and concurrency inside the request path.** Each conversion is a
  full office process in the same pod as the MCP server. #1265 needed its own
  concurrency limiter and output-size cap to keep that from starving the pod.
- **One copy per consumer.** Every process that needs conversion bundles its
  own LibreOffice.

Collabora Online's `coolwsd` is LibreOffice as a network service. Its
stateless `POST /cool/convert-to/<format>` endpoint converts one uploaded file
and returns the bytes, with no storage, WOPI host or session involved.

- Many Nextcloud deployments already run it as Nextcloud Office.
- It owns its own process pool, pre-spawning, jailing, and batch priority
  (`per_document.batch_priority`).
- Any number of consumers can share one instance.

## Decision

Add `CollaboraProcessor` (`document_processors/collabora.py`). It converts a
legacy or ODF file to its OOXML counterpart through `convert-to`, then hands
the result to the native reader for that format:

| Source | Target | Reader |
|---|---|---|
| `.doc`, `.odt` | `.docx` | `DocxProcessor` |
| `.xls`, `.ods` | `.xlsx` | `XlsxProcessor` |
| `.ppt`, `.odp` | `.pptx` | `PptxProcessor` |

- **Container-to-container, never to PDF.** A converted file gets exactly the
  treatment of one that arrived as OOXML: tables, headings, speaker notes,
  sheets and ADR-037 picture captioning. For spreadsheets this also avoids
  #1265's measured loss from print-layout pagination. The cost is that a
  `.doc` gets no page numbers.
- **Configured by URL only.** `COLLABORA_URL` (plus `COLLABORA_TIMEOUT_SECONDS`,
  default 60). Unset, the processor is not registered, so these types report
  "no processor" once instead of failing a request per document. It registers
  at priority 15, above the optional `unstructured` (10), like the other OOXML
  readers.
- **Container signature checked before sending.** LibreOffice imports bytes it
  does not recognise as plain text, so coolwsd returns 200 with a "document" of
  garbage for anything it is handed (observed against CODE 26.04). A file
  claiming an OLE2 type must start with the OLE2 signature, and an ODF file with
  a zip header, or it is refused without a request.
- **Access control is coolwsd's.** `convert-to` is only served to clients
  matching `net.post_allow` (by default loopback and the private ranges, which
  covers compose networks and cluster pod CIDRs). A denied client gets 403; the
  error names the setting. `/hosting/capabilities` reports
  `convert-to.available` per client, which is what `health_check()` reads.
- **`.msg` stays in-process.** Outlook messages are OLE2 but not office
  documents, and LibreOffice cannot open them. #1265's olefile-based reader
  (`msg.py`/`_msg_reader.py`) is carried over unchanged and registered
  unconditionally.

Measured against a local CODE 26.04.4.1 container: a small `.doc`, `.xls` or
`.ppt` converts to OOXML in 0.1–0.3 s. A converted legacy `.doc` table
reaches `DocxProcessor` intact.

## Consequences

- No LibreOffice in this image. Operators who want legacy/ODF formats point
  `COLLABORA_URL` at a coolwsd they already run, or add one. docker-compose has
  a `collabora` profile, and CI has an `integration (collabora)` lane.
- Every conversion is a network round trip to a shared service, bounded by
  `COLLABORA_TIMEOUT_SECONDS`. An unreachable service fails that file with a
  `ProcessorError`, never the process.
- Indexing these types is still governed by `VECTOR_SYNC_INDEXABLE_MIME_TYPES`.
  Adding them there is an operator choice, and only makes sense where
  `COLLABORA_URL` is set (`.msg` excepted).
- Not addressed: `.rtf`, `.wpd` and other formats LibreOffice imports. They are
  one `CONVERSIONS` entry each if they turn out to matter.
