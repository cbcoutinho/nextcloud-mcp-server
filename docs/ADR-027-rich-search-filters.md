# ADR-027: Rich Search Filters for Semantic Search

**Status**: Proposed
**Date**: 2026-06-02
**Depends On**: ADR-012 (Unified Multi-Algorithm Search), ADR-014 (BM25 Search), ADR-019 (Verify-on-Read for Semantic Search)
**Tracking**: Astrolabe Cloud POC Deck card #177

## Context

`nc_semantic_search` today exposes one structured filter — `doc_types` — on top of the query
string. Everything else a user might want to narrow by (when a document was last modified, which
folder it lives in, which tags it carries) is invisible to the search layer. As Astrolabe's corpus
grows across Notes, Files (PDFs), Deck cards, and News items, a single relevance ranking over the
whole index is increasingly blunt: the user knows "the spec I edited last week, somewhere under
/Projects" but can only type words and hope.

The Astrolabe PHP app surfaces semantic search through a plain `NcTextField` plus a doc-type
checkbox grid (`astrolabe/src/App.vue`). There is no visual affordance for any other dimension. We
want to add **rich, visually-indicated filters** — modelled on Nextcloud Unified Search's
filter-*chip* interaction — and weave them through the search backend without disturbing the
existing fusion + verify-on-read pipeline.

This ADR defines:

1. The **contract** for how a structured filter travels from the MCP tool signature down to a
   Qdrant `FieldCondition` (so every future filter follows one pattern).
2. The **payload-readiness** of each desired filter, which drives a phased rollout.
3. What the **frontend** sends and how it presents active filters.

### How filtering works today (the pattern to generalise)

A single filter — `doc_type` — already threads through three layers. New filters mirror it exactly.

1. **MCP tool signature** — `nextcloud_mcp_server/server/semantic.py` (`nc_semantic_search`) accepts
   `doc_types: list[str] | None` and dispatches one `search_algo.search(...)` call per type (or one
   call with `doc_type=None` for cross-app search).
2. **Algorithm** — `nextcloud_mcp_server/search/bm25_hybrid.py` `search()` receives `doc_type` and
   builds the Qdrant filter:

   ```python
   filter_conditions = [
       get_placeholder_filter(),                       # exclude pending placeholders
       build_ownership_filter(user_id, accessible_owners),  # ACL
   ]
   if doc_type:
       filter_conditions.append(
           FieldCondition(key="doc_type", match=MatchValue(value=doc_type))
       )
   query_filter = Filter(must=filter_conditions)
   ```

3. **Qdrant query** — `query_filter` is passed to **both** the dense and sparse `Prefetch` branches
   of the `query_points` call, so the filter applies *before* fusion. Filtering before fusion (not
   after) keeps the `limit * 2` candidate pools meaningful and avoids returning fewer than `limit`
   results when a filter is selective.

`build_ownership_filter` (`search/access_filter.py`) and `get_placeholder_filter`
(`vector/placeholder.py`) demonstrate the full matcher vocabulary we will reuse: `MatchValue`
(exact), `MatchAny` (OR-list), `Range` (numeric bounds), and `Filter(must=...)` / `Filter(should=...)`
for AND / OR composition.

### Payload readiness governs what we can ship

Filters can only be applied to fields that exist in the Qdrant payload (built in
`nextcloud_mcp_server/vector/processor.py`). Auditing the payload schema:

| Desired filter | Payload field | Type | Status |
|---|---|---|---|
| Modified-date range | `modified_at` | `int` (Unix ts) | ✅ **Ready** — numeric, range-filterable today |
| Document type | `doc_type` | keyword-indexed `str` | ✅ Implemented |
| Directory / path | `file_path` (files only) | `str` | ⚠️ Stored but **not keyword-indexed** — prefix/match needs a payload index |
| Tags | — | — | ❌ **Not indexed** — no `tags` field is written during scanning |
| Category (notes) | — | — | ❌ Not in payload — fetched from the Notes API at verify time only |

Two consequences:

- **`modified_at` is the cheap win.** It is already a numeric Unix timestamp on every point, so a
  `Range` condition works against the existing index with no re-index.
- **Tags / path / category are not free.** `file_path` filtering needs a Qdrant payload index
  before `MatchText`/prefix matching is performant; `tags` and `category` are not in the payload at
  all and require extending `processor.py` plus a full re-index. Conflating these with the date
  filter would make a small UX improvement wait on an expensive indexing migration.

## Decision

### 1. Generalise the filter contract

Every structured filter follows the `doc_type` path: **tool parameter → `search()` keyword arg →
`FieldCondition` appended to `filter_conditions` → `Filter(must=[...])` on both prefetch branches.**
Filters are always applied at the Qdrant layer, **before** verify-on-read (ADR-019), so that
`verified_chunk_count` / `dropped_document_count` describe the already-filtered set and the verifier
never wastes Nextcloud round-trips on documents the filter excluded.

Date/range bounds use `qdrant_client.models.Range`:

```python
from qdrant_client.models import FieldCondition, Range

if modified_after is not None or modified_before is not None:
    filter_conditions.append(
        FieldCondition(
            key="modified_at",
            range=Range(gte=modified_after, lte=modified_before),  # None bounds are open-ended
        )
    )
```

`Range` treats `None` bounds as open, so the same condition serves after-only, before-only, and
both-bounds queries. Validation that `modified_after <= modified_before` lives in the Pydantic
request model, not the algorithm.

### 2. Phase the rollout by payload readiness

- **Phase 1 — modified-date range (this ADR's committed scope).** Add `modified_after` /
  `modified_before` (Unix seconds, UTC) to `nc_semantic_search` and `bm25_hybrid.search()`. No
  re-index. Ship the frontend chip UX against this plus the existing doc-type filter to prove the
  end-to-end plumbing on fields that already exist.
- **Phase 2 — directory / path.** Create a Qdrant payload index on `file_path`, add a `path_prefix`
  parameter, and add an `NcFilePicker` folder chooser. Scoped to `doc_type == "file"`.
- **Phase 3 — tags (and optionally category).** Add a `tags: list[str]` payload field in
  `processor.py`, propagate Nextcloud system tags during scanning, trigger a re-index, then wire
  `NcSelectTags` (`MatchAny` over tags). Re-index cost lives here, isolated from the cheap wins.

### 3. Frontend: filter chips, structured payload

The Astrolabe app adds filter controls to the existing collapsible advanced panel and renders each
**active** filter as a closable `NcChip` (the same component Nextcloud Unified Search uses):

- Modified-date range → `NcDateTimePicker type="datetime-range"` (model is `[Date, Date]`).
- Doc types → existing checkbox grid, now also echoed as chips.
- (Phase 2/3) path → `NcFilePicker`; tags → `NcSelectTags :fetch-tags`.

The `/apps/astrolabe/api/search` endpoint moves from `GET` with query params to **`POST` with a JSON
body**, because the filter set is structured and multi-valued and will keep growing. Dates are sent
as **Unix seconds (UTC)** to match the `modified_at` payload representation exactly — no timezone or
string-parsing ambiguity crosses the wire. Empty or partially-filled filters are omitted from the
body rather than sent as nulls.

## Consequences

**Positive**

- One filter pattern for the whole search surface; adding a filter is a localized, testable change.
- Phase 1 ships immediately with zero re-index risk and proves the UX contract end-to-end.
- Filtering before verify-on-read keeps ACL/ghost semantics intact and avoids wasted verification
  round-trips.
- The chip UX matches Nextcloud conventions, so it reads as native to users.

**Negative / costs**

- Path and tag filters require index work (a payload index; a new payload field + full re-index)
  that this ADR explicitly defers — the readiness table makes that cost visible rather than implicit.
- Moving `/api/search` to POST is a breaking change to that endpoint's contract; the PHP app and the
  MCP backend must ship together.
- Pre-fusion filtering on a very selective `Range` can still under-fill `limit` if the candidate
  pool (`limit * 2`) is exhausted; if this proves a problem we revisit the prefetch multiplier
  rather than filtering post-fusion.

**Neutral**

- `SemanticSearchResponse` is unchanged — filters live entirely in the request. MCP clients that
  ignore the new parameters behave exactly as before (backward compatible).

## Alternatives Considered

- **Free-text `key:value` query parsing** (`modified:>2026-01-01 path:/Projects`). Powerful but
  invites injection-shaped ambiguity and a parser to maintain, and gives no visual affordance for
  "what can I filter by?". Structured params + chips answer the user's discoverability question
  directly. Could be layered on later as sugar over the same params.
- **Post-fusion / client-side filtering** (like the current score-threshold slider). Simple, but
  defeats the point of a recall layer: the index would return mostly-irrelevant candidates that get
  thrown away, and `limit` becomes unpredictable. Rejected in favour of pushing filters into Qdrant.
- **Indexing everything up front** so all filters ship at once. Forces a large re-index and couples
  a cheap UX win to an expensive migration. Rejected in favour of the readiness-driven phasing.
