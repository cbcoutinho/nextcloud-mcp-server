# ADR-040: Redacted export archives for subject access requests

## Status

Proposed — 2026-09-24. Replaces an earlier, unmerged design that redacted every
search and read surface at read time.

## Context

Organisations answering a Subject Access Request (GDPR Art. 15) must find what
they hold about one person and disclose it **without** other people's personal
data (Art. 15(4)). Astrolabe already makes the finding part tractable: the index
is searchable by the people and agents who answer the request.

Those operators are internal and already have access to the originals, so search
and read stay unredacted. What must be redacted is the **export**: the copies
that leave the organisation. The scope here is only retrieval and redaction.
Receiving the request, deadlines and delivery to the data subject happen outside
Astrolabe, and an internal auditor inspects every archive before it is shared.

## Decision

### Workflow

1. **Search and select.** The operator searches and picks the documents to
   disclose, each with a reason and an optional page range. Only included
   documents are recorded. How relevance scores and the queries themselves
   should drive inclusion is left to a follow-up.
2. **Submit.** The operator supplies the subject's identifiers (names, aliases,
   emails, phone numbers, NI numbers: the **keep list**), the items, optionally
   the queries that were run, and an **output folder**, typically a shared team
   folder. The server refuses a folder the user cannot write to, before any work
   starts.
3. **Redact**, asynchronously, with a status the operator can poll.
4. **Ready for audit.** The archive is in the output folder.

### Archive

```
<output folder>/
├── <name>.zip
│   ├── index.pdf       per document: number, redacted title, reason, pages, redaction counts;
│   │                   failed documents with the reason
│   ├── documents/      one PDF per document, e.g. 002-[PERSON_3]-letter.pdf
│   └── searches.pdf    the queries, when supplied
└── <name>.status.json  job status: counts only, never names
```

- Output is extracted text rendered to PDF. Original layout is not preserved.
- Titles, filenames and reasons are redacted along with the text; filenames
  routinely carry third parties' names, and a reason can repeat one.
- Placeholders are numbered across the whole archive: `[PERSON_3]` is the same
  person in every file.
- Original paths and file ids are never written to the archive.
- A document that cannot be read or redacted is listed as failed, never dropped.

### Redaction

- **Text comes from the index.** Every chunk stores its full text and its
  character offsets, so a document (or a page range of it) is reassembled
  without re-parsing or re-running OCR, and it is exactly the text the operator
  searched. Each item's access is checked for the requesting user first.
- **Detect once, match everywhere.** Person names are detected by the embedding
  gateway's `POST /v1/ner` over every item before anything is written, so one
  name set covers the archive. Redaction is then word-boundary matching of that
  set: a name detected once is replaced wherever it occurs, and each token of a
  multi-token name is replaced on its own, so a bare surname is caught.
- **Emails, phone numbers and NI numbers** are found by pattern.
- Everything not on the keep list becomes `[PERSON_n]`, `[EMAIL_n]`,
  `[PHONE_n]` or `[NI_n]`.
- Detection failure is never degraded around: the affected document is marked
  failed rather than exported unredacted.

### Surfaces

- MCP: `sar_export_submit` and `sar_export_status`.
- HTTP `/api/v1/sar/*` for the Astrolabe app, added with its UI.
- Available only with `EMBEDDING_GATEWAY_URL` configured.

### Execution

The export runs in-process in the MCP server as a background task, with
credentials resolved the way the ingest worker resolves them (single-user
environment credentials, or the user's stored app password). The status file in
the output folder is the durable record, so status is readable from any replica.
A job interrupted by a restart shows as stale and is resubmitted. A dedicated
queue is the upgrade path once usage warrants it.

## Consequences

- **NER recall is below 100%.** Propagation and token expansion narrow the gap;
  the auditor is the backstop, and per-document redaction counts in the index
  point that review at the right places.
- **Over-redaction is expected**, archive-wide: a common word tagged as a name
  is replaced in every document.
- **OCR-damaged names** are caught only if detected in that damaged form.
- **The export reflects the index.** A document changed since it was indexed is
  exported as indexed.
- **Person names and three identifier kinds only.** Addresses and other
  free-text personal data are out of scope for now.
- **Throughput** depends on the NER backend: CPU inference is two orders of
  magnitude slower than a GPU. `NER_BATCH_SIZE` and `NER_TIMEOUT_SECONDS` tune
  requests to the backend.
