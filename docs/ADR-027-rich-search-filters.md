# ADR-027: Rich Search Filters for Semantic Search

**Status**: Accepted (Phases 1–2 implemented; Phase 3 deferred)
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
| Modified-date range | `modified_at` | `int` (Unix ts) | ✅ **Ready** — value present on every point; needs a payload index (added in Phase 1, no content re-index) |
| Document type | `doc_type` | keyword-indexed `str` | ✅ Implemented |
| Directory / path | `file_path` (files only) | `list[str]` (multi-folder) | ✅ **Implemented (Phase 2)** — TEXT payload index (no content re-index); one or more folders filtered with `MatchText`, multiple OR-ed via nested `Filter(should=...)`; picked from the native folder browser |
| Tags | — | — | ❌ **Not indexed** — no `tags` field is written during scanning |
| Category (notes) | — | — | ❌ Not in payload — fetched from the Notes API at verify time only |

`modified_at` is a deliberate cross-app normalization the scanner already performs: the Notes API
and Deck return int Unix seconds natively, News timestamps are unit-normalized to seconds, and
WebDAV's RFC-1123 `getlastmodified` *string* is parsed to `int(dt.timestamp())`
(`client/webdav.py`). One `int` field therefore covers every doc type.

Two consequences:

- **`modified_at` is the cheap win — but it still needs a payload *index*.** The value is on every
  point, so **no content re-index** is required. However, Qdrant only evaluates a `Range`
  efficiently against an *indexed* field; without an index every dated query full-scans the
  collection (and 400s on Qdrant Cloud strict payload-validation mode). Phase 1 therefore adds a
  single `modified_at: INTEGER` entry to `_PAYLOAD_INDEX_FIELDS`
  (`vector/qdrant_client.py`); the existing idempotent `_ensure_payload_indexes()` startup path
  creates it on new **and** existing collections with no operator action. `INTEGER` (not `FLOAT`)
  because the stored value is an int Unix-second timestamp.
- **Why not a Qdrant `datetime` index?** Qdrant's `datetime` index / `DatetimeRange` would let us
  filter with RFC 3339 strings directly, but its indexer only ingests *string* payload values
  (`value.as_str()` in Qdrant's `numeric_index/value_indexer.rs`) — it silently skips integer
  payloads. Adopting it would mean re-storing `modified_at` as RFC 3339 strings on every point,
  i.e. the full re-index Phase 1 is designed to avoid. We instead keep int storage + a numeric
  `Range` and accept RFC 3339 only at the request boundary (see §3).
- **Tags / path / category are not free.** `file_path` filtering needs a Qdrant payload index
  before `MatchText`/prefix matching is performant; `tags` and `category` are not in the payload at
  all and require extending `processor.py` plus a full re-index. Conflating these with the date
  filter would make a small UX improvement wait on an expensive indexing migration.

## Decision

### 1. Generalise the filter contract through one shared helper

Every structured filter follows the `doc_type` path: **tool parameter → explicit `search()`
keyword arg → `FieldCondition` in the shared `filter_conditions` builder → `Filter(must=[...])`
on both prefetch branches.** Filters are always applied at the Qdrant layer, **before**
verify-on-read (ADR-019), so that `verified_chunk_count` / `dropped_document_count` describe the
already-filtered set and the verifier never wastes Nextcloud round-trips on documents the filter
excluded.

**The filter is added to the `SearchAlgorithm` ABC contract, not just one algorithm.** The two
algorithms that back the search surfaces — `BM25HybridSearchAlgorithm` (the MCP tool path,
`nc_semantic_search`) and `SemanticSearchAlgorithm` (the dense-only visualization / `/api/v1`
path, `api/visualization.py` + `auth/viz_routes.py`) — today build a *byte-identical*
placeholder + ownership + `doc_type` filter block. Rather than copy the new condition into both,
the common block moves into one helper, `search/access_filter.py::build_base_filter_conditions`,
and both algorithms call it. New filters are therefore honoured on **every** path (hybrid and
dense-only) by editing one function:

```python
# search/access_filter.py — the single source of the filter contract
def build_base_filter_conditions(
    user_id, accessible_owners=None, doc_type=None,
    modified_after=None, modified_before=None,
) -> list[Condition]:
    conditions = [get_placeholder_filter(), build_ownership_filter(user_id, accessible_owners)]
    if doc_type:
        conditions.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
    if modified_after is not None or modified_before is not None:
        conditions.append(
            FieldCondition(
                key="modified_at",
                range=Range(gte=modified_after, lte=modified_before),  # None bounds are open-ended
            )
        )
    return conditions
```

`Range` treats `None` bounds as open, so the same condition serves after-only, before-only, and
both-bounds queries. New range/match filters are added here once; each algorithm wraps the
returned list in `Filter(must=...)` and may append its own additive conditions afterward (e.g.
`SemanticSearchAlgorithm`'s opt-in ACL pre-filter, which `BM25HybridSearchAlgorithm` does **not**
apply).

`modified_after` / `modified_before` are promoted to **explicit named keyword params** on
`SearchAlgorithm.search()` (and both concrete impls), exactly as `accessible_owners` was — not
left in `**kwargs`. Explicit params keep them discoverable and make a misspelled keyword a type
error instead of a silently-ignored filter. When `doc_types` is a list, the tool's per-type
dispatch loop forwards the same `modified_after`/`modified_before` to each per-type
`search()` call.

**Input validation.** FastMCP exposes no request-model object to hang a Pydantic
`@model_validator` on, so validation is split across three layers, each handling what it can
express:

- **Per-argument scalar bounds** use `Annotated[..., Field(...)]` on the tool signature — FastMCP
  builds the input schema from these and rejects bad values before the body runs. This is the
  repo's first use of `Annotated`/`Field` on a tool; the existing numeric knobs (`limit` `ge=1`,
  `score_threshold` `0.0–1.0`, `context_chars` `ge=0`) are tightened the same way and the pattern
  is what future filters reuse.
- **Format normalization for the date bounds.** `modified_after` / `modified_before` are typed
  `str | int | None` and accept an **RFC 3339 / ISO 8601 datetime** (e.g. `"2026-01-01T00:00:00Z"`,
  naive ⇒ UTC) *or* Unix seconds. A shared `utils/validation.py::parse_modified_timestamp` helper
  normalizes either to int Unix seconds (the payload representation) and raises `ValueError` on an
  unparseable value; the tool converts that to `McpError`, the HTTP endpoints to a 400. The same
  helper is reused by `nc_semantic_search`, the `/api/v1` search endpoints, and the viz route so
  every surface accepts identical formats.
- **The cross-field invariant `modified_after <= modified_before`** can't be a per-field
  constraint, so it is an explicit guard (on the *parsed* int values) that raises
  `McpError(ErrorData(code=-1, ...))` in the tool — the established input-error idiom (mirrors the
  `VECTOR_SYNC_ENABLED` guard) — and a 400 on the HTTP endpoints.

### 2. Phase the rollout by payload readiness

- **Phase 1 — modified-date range (this ADR's committed scope).** Add `modified_after` /
  `modified_before` (RFC 3339 / ISO 8601 at the boundary, Unix seconds accepted too) to the
  `SearchAlgorithm` contract and both algorithm impls (so the MCP tool *and* the dense-only
  `/api/v1` path honour it), plus a `modified_at` INTEGER payload index. No content re-index. Ship
  the frontend chip UX against this plus the existing doc-type filter to prove the end-to-end
  plumbing on fields that already exist.
  - **`nc_semantic_search_answer` is explicitly deferred, not part of Phase 1.** It is a thin RAG
    wrapper that today threads only `query`/`limit`/`score_threshold`/`fusion`/context options to
    `nc_semantic_search` and does not even expose `doc_types` — it always searches cross-app. Date
    scoping is a search-box affordance, not an answer-synthesis one, and there is no UI surface for
    it on the answer path. When demand appears it can be threaded through using exactly the same
    parameter + `parse_modified_timestamp` pattern; doing it now would add an unused parameter and
    widen the change for no user-visible gain.
- **Phase 2 — directory / path (implemented).** Threaded through the same shared contract
  (`build_base_filter_conditions`) and backed by a `file_path` **TEXT** payload index in
  `_PAYLOAD_INDEX_FIELDS` (no content re-index — `file_path` is already on every file point).
  `MatchText` semantics differ by backend: **server Qdrant** tokenizes (AND-of-tokens, so
  `/Projects/Reports` matches files whose path contains both the `Projects` and `Reports` tokens),
  while **local/embedded qdrant-client** matches by substring containment — both serve folder
  scoping, neither is a strict left-anchored prefix (a future strict-prefix would need an indexed
  ancestor-path array, i.e. a re-index, so it stays out of Phase 2). Because `file_path` is only
  written for `doc_type == "file"`, a non-empty path filter implicitly restricts results to files.
  - **Multi-folder (list-valued).** The filter accepts **one or more** folders via a
    `path_prefixes: list[str]` parameter (the original single `path_prefix` is retained for
    backward compatibility and folded into the list by `normalize_path_prefixes`, which trims,
    drops blanks, and de-dupes). `build_base_filter_conditions` adds a single `MatchText` to the
    `must` clause for one folder, and OR-s multiple folders via a nested `Filter(should=[...])` so a
    file under **any** selected folder matches while still AND-ing against the ACL/doc_type/date
    conditions. Every search surface parses the list: the MCP tool (`nc_semantic_search`) takes a
    real `list[str]`, the visualization API takes a JSON array body, and the viz route takes a
    **newline-separated** query param. Newline (not comma) is the on-the-wire delimiter because it
    can't appear in a POSIX path, so folder names are never split mid-value.
  - **Frontend uses the native folder picker.** Instead of a free-text path input, the Astrolabe
    app opens Nextcloud's server-side folder browser via `getFilePickerBuilder()` from
    `@nextcloud/dialogs` (already a dependency — no `@nextcloud/vue` component-version coupling),
    configured directory-only + multi-select. Picked folders are real, validated server paths
    (no typos), rendered as removable chips, and sent as a newline-joined `path_prefixes` value. The
    Astrolabe PHP `ApiController` splits on newline (capping the list to bound the OR-filter width)
    and `McpServerClient` forwards a JSON array to the MCP server. The control
    is enabled only when the **Files** doc type is in scope; an empty selection means "no filter".
- **Phase 3 — tags (and optionally category).** Add a `tags: list[str]` payload field in
  `processor.py`, propagate Nextcloud system tags during scanning, trigger a re-index, then wire
  `NcSelectTags` (`MatchAny` over tags). Re-index cost lives here, isolated from the cheap wins.

### 3. Frontend: filter chips, structured payload

The Astrolabe app adds filter controls to the existing collapsible advanced panel and renders each
**active** filter as a closable chip:

- Modified-date range → two native `<input type="datetime-local">` fields (a local wall-clock
  picker the browser validates), serialized to RFC 3339 UTC via `Date.toISOString()`. Phase 1
  deliberately uses native inputs + lightweight CSS chips rather than taking a hard dependency on a
  specific `@nextcloud/vue` component version (`NcDateTimePicker` / `NcChip` remain a later option);
  this matches the component's existing native `<input type="range">` controls.
- Doc types → existing checkbox grid, now also echoed as chips.
- (Phase 2) path → native folder picker (`getFilePickerBuilder` from `@nextcloud/dialogs`),
  multi-select, rendered as one removable chip per folder. (Phase 3) tags → `NcSelectTags
  :fetch-tags`.

Dates cross the wire as **RFC 3339 / ISO 8601 strings** (e.g. `"2026-01-01T00:00:00Z"`) — the format
Nextcloud's date pickers and Unified Search use. The MCP/HTTP boundary parses RFC 3339 (and bare
Unix seconds, for resilience) to int Unix seconds via `parse_modified_timestamp` before filtering,
so the friendly wire format never forces a re-index of the integer `modified_at` payload.

**Transport.** Phase 1 keeps the existing `GET /apps/astrolabe/api/search`: the two scalar date
strings ride as query params alongside the already comma-joined `doc_types`, so no transport change
is needed yet. The move to **`POST` with a JSON body** is deferred to Phase 2, when the filter set
becomes genuinely structured/multi-valued (path + tags) and outgrows query params.

The **Astrolabe UI validates the date range client-side** — the native picker constrains each field
to a real datetime, and `performSearch` rejects an *after > before* range before issuing the request
(`McpServerClient` forwards the RFC 3339 strings; `ApiController` re-validates and returns a 400 on a
bad/inverted range). The server-side `parse_modified_timestamp` + cross-field guard is the
authoritative backstop. (Astrolabe-side work is tracked on Deck card **#177**, label
`repo:astrolabe`.)

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
- The eventual `/api/search` GET→POST migration (Phase 2) will be a breaking change to that
  endpoint's contract; the PHP app and the MCP backend must ship together when it lands. Phase 1
  avoids it by keeping GET.
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
