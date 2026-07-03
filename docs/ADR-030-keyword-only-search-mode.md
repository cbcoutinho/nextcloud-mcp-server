# ADR-030: Keyword-only (airgapped) search mode

## Status

Accepted — 2026-06-30

## Context

Cross-app search is gated by a single flag, `ENABLE_SEMANTIC_SEARCH` /
`VECTOR_SYNC_ENABLED`:

- **On** → the vector-sync pipeline scans content, chunks it, and generates
  **both** dense embeddings (which require an external embedding endpoint —
  Ollama, Bedrock, OpenAI, Mistral, or the embedding gateway) **and** BM25
  sparse vectors (computed in-process, no endpoint). `nc_semantic_search` runs a
  hybrid dense+sparse query via Qdrant's native `FusionQuery`
  (`search/bm25_hybrid.py`).
- **Off** → no Qdrant, no vector tools; only Nextcloud-native per-app keyword
  search (`nc_notes_search_notes`, `nc_webdav_search_files`).

There is no middle ground for an operator who wants the **cross-app,
Qdrant-indexed full-text search** (notes, files, OCR'd PDFs, deck cards, news,
mail) but **cannot run a text-embedding endpoint** — e.g. a fully airgapped
deployment, or one that simply does not want the cost/operational surface of an
embedding model.

The enabling fact: **BM25 sparse vectors need no external endpoint.** They are
computed locally (`vector/processor.py` `generate_sparse_embeddings`,
`embedding/service.py` `get_bm25_service`). So we can keep the entire Qdrant
ingestion path — including verify-on-read ACLs (ADR-019) — and just skip the
dense step at ingestion and query.

## Decision

Add a config enum **`SEARCH_MODE` = `hybrid` (default) | `keyword`** (ADR-030),
read through the existing Dynaconf + `Settings` machinery
(`config.py`). A single source of truth, `Settings.dense_enabled`
(`search_mode != "keyword"`), is consulted by the three affected subsystems.

In `keyword` mode:

1. **Ingestion** (`vector/processor.py`) skips `generate_dense_embeddings`
   entirely and upserts **sparse-only points**.
2. **Query** (`search/bm25_hybrid.py`) skips the dense query embedding and
   issues a **direct sparse query** (no prefetch, no fusion).
3. The `nc_semantic_search` and `nc_semantic_search_answer` tools stay
   registered (relabeled as keyword-backed). The RAG answer tool's generation
   runs client-side via MCP sampling, so it needs no server-side LLM.

`keyword` mode **still requires vector sync enabled** — it uses the Qdrant
index. With vector sync off, the tools simply don't register and a startup
warning is logged (gate-don't-crash, matching the existing posture).

### Sparse-only *points* on the existing dense+sparse schema

Qdrant lets a point carry a **subset** of a collection's named vectors. We keep
the collection schema unchanged (`vectors_config={"dense": ...}` +
`sparse_vectors_config={"sparse": ...}`) and upsert points with only the
`sparse` vector in keyword mode. This was chosen over a sparse-only collection
schema because the latter would force edits across `placeholder.py`,
`dead_letter.py`, `collection_metadata.py`, the doc-id sentinel, and the
dimension-validation branch — far more blast radius for no functional gain.

The dense named vector still has to be *sized* at collection creation. In
keyword mode the size comes from the **local `SimpleProvider` dimension**
(`SIMPLE_EMBEDDING_DIMENSION`, default 384), never from a network probe — the
Ollama `_detect_dimension()` call is explicitly skipped
(`vector/qdrant_client.py`) so collection creation stays endpoint-free even if a
stray `OLLAMA_BASE_URL` is set.

**Invariant:** in keyword mode, nothing on the ingest or query path calls
`embedding_service.embed*` or the Ollama dimension probe.

### Score semantics

In hybrid mode the result `score` is a normalized fusion score (RRF ∈ [0, 1];
DBSF can exceed 1). In keyword mode it is a **raw BM25 score (unbounded)**. The
default `score_threshold=0.0` is safe in both, but:

- `nc_semantic_search_answer` defaults `score_threshold` to `0.7`, calibrated
  for fusion. In keyword mode an untouched `0.7` default is treated as `0.0`
  (an explicit caller value still wins), so it does not silently drop all BM25
  matches.
- The `fusion` parameter is meaningless in keyword mode (no fusion happens). It
  is kept for API stability and ignored.

`search_method` in the response is `bm25_hybrid_<fusion>` in hybrid mode and
`bm25_keyword` in keyword mode, so responses are self-describing.

### Collection segregation & migration

Keyword and hybrid indexes are **not interchangeable** (their point vector sets
differ). `get_collection_name()` returns a mode-marked name
(`{deployment}-bm25-keyword`) in keyword mode, so flipping modes targets a fresh
collection by default and removes keyword mode's dependency on the (phantom)
embedding model name. Reusing an explicit `QDRANT_COLLECTION` across modes trips
the existing "Dimension mismatch" guard.

Keyword-only points are stamped with the value `"bm25-keyword"` on the
`EMBEDDING_IDENTITY` payload key (the key name is defined in
`vector/payload_keys.py`; the sentinel value is written in
`vector/processor.py`), so mixed-mode contamination of a collection is auditable
by scrolling that payload key. (Note: the external processor — sibling
repo — also writes `EMBEDDING_IDENTITY`; this sentinel value is additive and
MCP-server-local.)

**To switch modes:** use a new collection (or clear the old one) and let
background vector sync re-ingest. No in-place migration is supported.

### Advertising supported query types to the astrolabe UI

So the astrolabe UI can gate which query types it offers without knowing the
server's `SEARCH_MODE`, `GET /api/v1/status` advertises a
`supported_search_types` array (`api/management.py` `supported_search_types`):

- vector sync disabled → `[]`
- `SEARCH_MODE=keyword` → `["bm25"]`
- `SEARCH_MODE=hybrid` → `["semantic", "bm25", "hybrid"]`

The vocabulary (`semantic` | `bm25` | `hybrid`) is the same `algorithm` the
astrolabe `McpServerClient` already passes to `/api/v1/search`; it lives in the
single constant `SUPPORTED_SEARCH_ALGORITHMS`, reused by the
`/api/v1/vector-viz/search` validation so the advertised set and the accepted
set cannot drift.

#### Strict rejection of an explicit unsupported algorithm

Advertising is only half the contract — the search endpoints enforce it. When a
client sends an **explicit** `algorithm` that is not in `supported_search_types`
(the paradigm case: `algorithm: "semantic"` while `SEARCH_MODE=keyword`), both
`/api/v1/search` and `/api/v1/vector-viz/search` return **HTTP 422**:

```json
{ "error": "unsupported_search_type", "requested": "semantic",
  "supported_search_types": ["bm25"] }
```

This is deliberately stricter than the earlier behaviour (which silently coerced
`semantic` → `bm25`). A silent downgrade returns lexical results dressed up as a
semantic answer; a 422 lets astrolabe *and* any other client fail loud and
self-correct from the advertised set. The strictness applies only to an
**explicit** request: a call that omits `algorithm` still defaults gracefully
across modes. `api/management.py` splits the two paths — `select_search_algorithm`
(explicit → raise `UnsupportedSearchType`; absent → default) wraps the retained
`resolve_search_algorithm` (the lenient default-coercion helper).

Astrolabe is the **consumer** of `/api/v1/status` and `/api/v1/search`, and this
server is the **provider** (ADR-029). The contract is pinned both ways: a
contract-first consumer pact
(`tests/contract/test_mcp_status_search_types_consumer.py`, written to the
unpublished `provider_contracts/` dir) covering the status advertisement in each
mode **and** the keyword-mode 422, plus the matching provider-state handlers in
`tests/contract/test_mcp_provider_verification.py` (`the server advertises hybrid
search support` / `… keyword-only search support`). The real status response is
verified per mode by `tests/unit/test_management_status_endpoint.py`, and the
`select_search_algorithm` gate is unit-tested there too. The astrolabe PR
consumes `supported_search_types` to gate its query-type picker client-side and
surfaces the 422 as its server-side backstop.

## Consequences

- Fully airgapped deployments gain cross-app full-text search with no embedding
  endpoint, retaining the unified Qdrant index, OCR'd-PDF coverage, and
  verify-on-read ACLs.
- Keyword search is lexical only — no conceptual/semantic recall. Operators who
  want both keep the default `hybrid` mode.
- `nc_semantic_search_answer` still works airgapped (BM25 retrieval +
  client-side generation via MCP sampling).
- No schema change; existing hybrid deployments are unaffected (default stays
  `hybrid`).

## References

- ADR-014 — BM25 sparse search
- ADR-012 — unified multi-algorithm search
- ADR-015 — unified provider architecture (SimpleProvider fallback)
- ADR-019 — verify-on-read ACLs (unchanged by this ADR)
