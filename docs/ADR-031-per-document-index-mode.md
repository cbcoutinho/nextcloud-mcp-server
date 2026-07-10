# ADR-031: Per-document keyword vs hybrid index mode

## Status

Accepted — 2026-07-08. Supersedes [ADR-030](./ADR-030-keyword-only-search-mode.md).

## Context

ADR-030 introduced a **global** `SEARCH_MODE` switch (`hybrid` | `keyword`)
surfaced as `Settings.dense_enabled`. In `keyword` mode the whole deployment
skipped dense embeddings and routed to a separate,
non-interchangeable `{deployment_id}-bm25-keyword` collection.

That is too coarse. A tenant frequently wants **both**: pay for dense embeddings
on high-value PDFs (semantic + keyword search) while cheaply lexically-indexing
bulk/low-value PDFs (keyword only, no embedding cost) — and search **all of it in
one unified result set**. A global switch cannot express this; it forces the
whole index into one mode and splits keyword vs hybrid across two collections
that cannot be queried together.

Enabling fact: a Qdrant collection can define both a `dense` and a `sparse` named
vector, and a point may populate a **subset** of them. So the dense-vs-sparse
decision can move from a deployment-global flag to a **per-document** one within
a single collection — hybrid documents write `dense` + `sparse`, keyword-only
documents write `sparse` only.

## Decision

**Remove** `SEARCH_MODE` / `Settings.dense_enabled` and the `-bm25-keyword`
collection entirely. Make the mode **per-document**, driven by which Nextcloud
tag selected the document:

- **`vector_sync_pdf_tag`** (default `vector-index`) → **hybrid**
  (dense + BM25 sparse).
- **`vector_sync_keyword_tag`** (env `VECTOR_SYNC_KEYWORD_TAG`, default
  `keyword-index`, symmetric with the hybrid tag; set empty to disable) →
  **keyword** (BM25 sparse only).

The collection is always sized for a real dense slot (from the embedding model);
keyword documents simply omit the dense vector per-point. A new payload field
`index_mode` (`payload_keys.INDEX_MODE`, values `hybrid` | `keyword`) records the
mode on every chunk point.

### Ingestion

- The scanner discovers both tags (`_discover_tagged_files`), stamping each file
  with `_index_mode`. **Hybrid wins** when a file carries both tags (it is a
  superset of keyword). The keyword tag is queried only when configured, so
  single-tag deployments issue exactly one OCS Tags query as before.
- `DocumentTask.index_mode` carries the mode to the processor (default `hybrid`,
  so every non-file producer — notes, deck, news, mail — is unchanged).
- The processor computes `dense_for_doc = index_mode != "keyword"` and only runs
  `generate_dense_embeddings` for hybrid documents; keyword documents upsert
  sparse-only points.
- The SystemTag webhook reconcile (`_reconcile_tag_event`) resolves against both
  tags and sets `index_mode` from whichever matched (hybrid precedence).

### Embedding failures are hard errors (no silent degrade)

With the global skip gone, a hybrid document **always** attempts dense
embeddings. A failed/unavailable embedding endpoint raises out of
`generate_dense_embeddings` into `process_document`'s retry → dead-letter path
(429s are retried inside the provider). It is **never** silently downgraded to a
sparse-only point. The only way a document is sparse-only is an explicit
`keyword-index` tag.

### Query

Unchanged and unified: `BM25HybridSearchAlgorithm` always fuses a dense + sparse
prefetch (RRF/DBSF). Keyword documents carry no dense vector, so the dense
prefetch never returns them; they surface via the sparse prefetch and are merged
in by fusion. A pure-`semantic` query naturally omits them.

### Cross-user dedup (monotonic toward hybrid)

`embedding_identity` remains the embedding **model** name (orthogonal to
keyword/hybrid) and still guards model switches. The keyword/hybrid distinction
is applied on top via `index_mode` in `find_indexed_content` /
`claim_existing_index`:

- a **hybrid** claim against an existing **keyword** point is a **miss** → the
  document is reprocessed and gains a dense vector (**upgrade**);
- a **keyword** claim against an existing **hybrid** point is a **hit** — hybrid
  ⊇ keyword, so the shared points are never downgraded while any user holds the
  hybrid tag;
- same-mode is a hit, as before.

The scanner's incremental path additionally reprocesses a real point whose stored
`index_mode` differs from the desired one (the keyword→hybrid upgrade at an
unchanged etag, which the mtime gate alone would miss).

### Verify-on-read

`search/verification.py` gates file results on membership of **either** tag
(union via `_discover_tagged_files`), so untagging from whichever tag indexed a
file drops it from results (ADR-019 semantics, extended to two tags).

### Billing

Ingestion metering moved out of the dense-only coroutine so **both** modes are
metered. `bytes_ingested` / `bytes_stored` carry an `index_mode` **metadata**
dimension (same metric names — no new metrics) so the control plane can slice
hybrid vs keyword ingestion. `tokens_embedded` is naturally hybrid-only (keyword
documents pass `token_count = 0`, which skips the row).

## Consequences

- **Breaking migration:** any existing `SEARCH_MODE=keyword` deployment used the
  `-bm25-keyword` collection and `bm25-keyword`-identity points, which are now
  orphaned. Such a deployment must **re-index** into the model-named collection.
- Airgapped, no-embedding deployments are still supported: configure no real
  embedding provider (the local `SimpleProvider` sizes the dense slot) and tag
  everything `keyword-index`; nothing ever calls an embedding endpoint.
- The Astrolabe Nextcloud app owns provisioning/exposing the `keyword-index` tag
  (tracked cross-repo on Deck board 12).
