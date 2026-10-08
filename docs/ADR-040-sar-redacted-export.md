# ADR-040: Redacted export archives for subject access requests

## Status

Proposed — 2026-09-24; partially superseded by
[ADR-041](ADR-041-document-basket.md) (proposed). Replaces an earlier,
unmerged design that redacted every search and read surface at read time.

ADR-041 supersedes the case model and surfaces: a case becomes a basket of
kind `sar_redact`. The archive and redaction rules here still apply.

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

### Workflow: a case

A SAR is a **case**, one shared, durable object that the Astrolabe app and MCP
agents both work on:

```
open ──export──▶ exporting ──done──▶ ready_for_audit ──close──▶ closed
  ▲                  │ failed                 │
  └──────────────────┴──────── reopen ────────┘
```

1. **Create** the case in a folder the user can write to, typically a team
   folder, with the subject's identifiers (names, aliases, emails, phone
   numbers, NI numbers, addresses: the **keep list**).
2. **Search and select.** Add documents with an optional reason and page
   range; remove them; log the searches run, including ones that found
   nothing. Only included documents are recorded. How relevance scores and the
   queries should drive inclusion is left to a follow-up.
3. **Export.** The case locks while a background job builds the archive, then
   becomes **ready for audit**. A failed export reopens it. Reopening to change
   and export again writes a new version (`-v1`, `-v2`, ...); earlier archives
   are kept.
4. **Close.** Final: the case becomes read-only and cannot be reopened. Its
   archives stay.

#### Where a case lives

`<folder>/<case name>/sar-case.json`, in Nextcloud rather than a database of
the MCP server:

- The subject's identifiers and the internal titles stay in the customer's
  system of record, with its backup, retention and erasure.
- Nextcloud's permissions are the access model: whoever can write the case
  folder can work on the case, and sharing a case means sharing its folder.
  File versions give an edit history.
- A case is addressed by its **case id**, the Nextcloud file id of
  `sar-case.json`, which survives moves and renames and is the same for every
  user the folder is shared with. Resolving it (WebDAV SEARCH by fileid) is
  also the access check, so a case the user cannot see is a 404.
- Every change is a read-modify-write guarded by the file's ETag. On a
  conflict the change is re-applied to the fresh copy, because changes are
  operations ("add these items"), not whole-document replaces.
- A case holds up to 2,000 documents and logs up to 1,000 searches. Past
  that, cases belong in a database table and exports need streaming. A search
  already in the log (same text and filters) is not logged again.

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
- **One person, one number.** A bare token shares its full name's number when
  it belongs to exactly one detected third party and to none of the subject's
  kept names, so "Karen Smith", "Karen" and "Smith" are all `[PERSON_1]`. A
  token shared by two people (or with the subject) keeps a number of its own,
  so the archive never attributes an ambiguous mention.
- **Addresses** are detected by NER as whole phrases (never split into words;
  single-word addresses such as a lone town are ignored), and UK postcodes by
  pattern. The subject's own address, or its leading part (from the house
  number), is kept; a bare street or town is not, since it may be someone else's.
- **Emails, phone numbers and NI numbers** are found by pattern.
- Everything not on the keep list becomes `[PERSON_n]`, `[ADDRESS_n]`, `[EMAIL_n]`,
  `[PHONE_n]` or `[NI_n]`.
- Detection failure is never degraded around: the affected document is marked
  failed rather than exported unredacted.

### Surfaces

The same operations over MCP (agents) and HTTP (the Astrolabe app), each acting
as the calling user:

| Operation | MCP tool | HTTP |
|---|---|---|
| Create | `sar_case_create` | `POST /api/v1/sar/cases` → 201 |
| List | `sar_case_list` | `GET /api/v1/sar/cases` |
| Get (items paged, latest export progress) | `sar_case_get` | `GET /api/v1/sar/cases/{id}` |
| Subject, description, close, reopen | `sar_case_update` | `PATCH /api/v1/sar/cases/{id}` |
| Add/update/remove items, log queries | `sar_case_items` | `POST /api/v1/sar/cases/{id}/items` |
| Export (optional output folder; default the case's `exports/`) | `sar_case_export` | `POST /api/v1/sar/cases/{id}/exports` → 202 |
| Search for the case, with the search page's filters; logged with them | `sar_case_search` | `POST /api/v1/sar/cases/{id}/search` (the `/api/v1/search` body) |

Every operation needs the `sar.read` (list, get) or `sar.write` (everything
else) scope, on the MCP token or the bearer token of the HTTP routes. Exporting
personal data about someone is its own grant, not a side effect of file access:
the tools also keep their underlying scopes (`files.*`, `semantic.read` for
search and export), so a SAR scope never widens what can be read. The SAR
scopes are advertised (DCR, the tools' scope list) only when `sar_available`,
and Astrolabe asks for them when minting the tokens for its SAR calls.

Refusals carry their status: 400 invalid, 403 folder not writable or no
background access, 404 no such case (or no access to it), 409 wrong state or
name taken, 503 no background task group. `GET /api/v1/status` advertises
`sar_available`; Astrolabe shows its SAR UI only when it is true. SAR is
opt-in per deployment: `SAR_ENABLED=true` (default false), plus semantic
search and `EMBEDDING_GATEWAY_URL`, which startup requires once it is set. The
HTTP routes additionally need an authenticated deployment mode.

The tools, routes, scopes and `sar_available` flag reach the server through a
single `Plugin` (`nextcloud_mcp_server/sar_plugin.py`), registered under the
`nextcloud_mcp_server.plugins` entry-point group; the server has no SAR-specific
wiring of its own. See `nextcloud_mcp_server/plugins.py`.

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
- **Names, addresses and three identifier kinds only.** Dates of birth, staff
  or student IDs and other personal data are out of scope for now.
- **Throughput** depends on the NER backend: CPU inference is two orders of
  magnitude slower than a GPU. `NER_BATCH_SIZE` and `NER_TIMEOUT_SECONDS` tune
  requests to the backend.
