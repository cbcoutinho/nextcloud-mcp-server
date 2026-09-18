# ADR-038: Redacted content view for subject access requests

## Status

Accepted — 2026-09-18. Delivered as a stack of PRs. The first adds the building
blocks (NER client, redaction core, settings). Later ones add the surfaces
listed under [Surfaces](#surfaces) and enforced mode.

## Context

Astrolabe's compliance users answer Subject Access Requests (GDPR Art. 15),
usually over scanned HR archives. Art. 15(4) shapes what may be disclosed: the
data subject's own personal data, **without** other people's. The full-text
index is what makes finding a subject's records possible in the first place, so
ingest and indexing stay exactly as they are.

Every agent-facing surface, however, returns full text today:
`nc_webdav_read_file` returns the whole parsed document, and `nc_semantic_search`
returns chunk excerpts, titles, deep-link URLs (which embed title and path) and
neighbouring context. `/api/v1/search` and `/api/v1/chunk-context` do the same.
An agent that prepares a SAR bundle therefore sees, and can repeat, every third
party's name.

The goal is a **redacted view** an agent can read: the data subject's name as
written, every other person replaced with `[PERSON_n]`.

## Decision

### Detect once, match everywhere

Named-entity recognition (NER) yields the **set of person names** in a
document. Redaction is then word-boundary matching of that set over whatever
text is about to be returned.

Storing character spans at ingest and replaying them was rejected. Each surface
slices text differently: search excerpts, neighbouring context, titles, file
paths, and `read_file`'s live re-parse, which can pick a different parse tier
than ingest did. Offsets computed once would not line up across them. Matching
by surface form works on all of them.

Two recall aids, both biased toward over-redaction, which is the acceptable
failure for a disclosure:

- **Propagation.** A name detected once is redacted at every occurrence in the
  document, including ones the model missed in context.
- **Token expansion.** Each token (3+ characters, not an honorific) of a
  multi-token name is redacted on its own, so a later bare "Smith" is caught.

Matching tolerates the separators real documents use between name tokens:
whitespace and line breaks (OCR, markdown), and `_`, `.`, `-` (filenames, email
local-parts). Boundaries are "not a letter or digit" rather than `\b`, so
`KAREN_SMITH.pdf` matches.

### Keeping the data subject

The caller passes `keep_names`: the subject's name and every alias to leave
visible (`"Jane Doe"`, `"Ms Doe"`, `"J. Doe"`). Keep aliases match first,
because the longest alternative wins. A detected name that is itself kept is
not token-expanded, so a bare "Jane" survives when only "Jane Doe" is the
subject. But if "Jane" is also a token of a third party's name, it is ambiguous
and redacted. Matching is case-insensitive but otherwise exact: an alias that
isn't listed is redacted.

Replacements are numbered per response, by canonical name. The same person is
`[PERSON_2]` in an excerpt and in the title beside it. A bare surname gets its
own number rather than the full name's; reconciling aliases into one person is
the job of a person-entity layer, not of string matching.

### Detection runs in the embedding gateway

Names are detected by the Astrolabe embedding gateway's `POST /v1/ner`
(`providers/ner.py`), next to OCR, embeddings and rerank:

```
request:  {model, texts: [str], labels: ["person"], threshold}
response: {results: [{index, entities: [{start, end, text, label, score}]}]}
```

- The model runs in the gateway (a GLiNER PII model by default; `NER_MODEL`),
  not in the MCP image, and on **always-on CPU**. Reads block on detection, so
  a burst GPU's cold start is unacceptable.
- The client takes each name from the *submitted* text by offset, never from
  the echoed `text`.
- Long texts are split into overlapping 2,000-character windows so a name
  straddling a cut is still seen whole.

### Optional, gateway-only, fail closed

- `CONTENT_REDACTION` = `off` (default) | `optional` | `enforced`. Without
  `EMBEDDING_GATEWAY_URL`, the **effective** mode is `off` whatever is
  configured, and a startup warning says so (`redaction.redaction_mode`).
- Asking for redaction where it is unavailable is an **error**, never a silent
  unredacted read.
- **Nothing unredacted escapes a redacted request.** An NER transport error,
  timeout, non-2xx or incomplete response raises `NerError`. The client is
  strict where the rerank client is lenient: a text its response did not
  account for would otherwise be treated as name-free. A result that cannot be
  redacted, such as base64 bytes, is refused.

### Surfaces

- **`nc_webdav_read_file(redact, keep_names)`.** Redacts the content, path and
  parse notes; withholds processor metadata (it can carry the author); and
  reports a `redaction` block on the response.
- **`nc_semantic_search(redact, keep_names)`**, **`/api/v1/search`** and
  **`/api/v1/chunk-context`.** At ingest, each chunk's payload stores the
  document's names that occur in that chunk. Excerpts, context, titles and URLs
  are redacted from those names. A point that has not been scanned gets live
  NER on its excerpt, and its excerpt is withheld if NER is unavailable.
  Results carry a `file_id`, because the path itself is redacted.
- **Enforced mode.** Principals without the `content.unredacted` scope are always
  redacted, and only tools marked redaction-safe stay visible to them (an
  allowlist, so a new tool is hidden until it opts in). The scope is a no-op,
  and is not advertised, unless redaction is available and enforced.
  Enforcement applies to MCP only: `/api/v1` serves the logged-in human, who
  can open the originals in Files anyway.

## Consequences

Residual risks, stated plainly because the feature supports a legal process:

- **NER recall is below 100%.** Propagation and token expansion narrow the gap
  but do not close it. A human still reviews a SAR bundle before disclosure.
- **Search can be used as an oracle.** Querying a third party's name, or passing
  it in `keep_names`, confirms which documents mention them. This is accepted
  because the caller is the SAR operator. A later option is a case object whose
  subject an admin pins.
- **OCR-damaged forms** (e.g. a scan that drops the space in "K Smith") are
  only caught if the model detected that exact damaged form.
- **Person names only.** Addresses, national insurance numbers and phone
  numbers are out of scope. The request's `labels` field leaves room to add
  them.
- **Over-redaction is expected.** A common word that the model tagged as a name
  is redacted everywhere in that document.
- **Performance.** On about 1 MB of text with 500 distinct names, matching takes
  a couple of seconds, which is small next to detection itself. A trie-compiled
  pattern is the upgrade path if that changes.
