# ADR-042: Image embeddings as a third named vector

## Status

Proposed — 2026-11-22. Tracked on Deck board 12, card #103. Builds on the
provider surface in PR #800 (GH #703).

## Context

Image files can already reach the index, but only as **text**. Discovery
indexes every MIME type some document processor claims
(`Settings.indexable_mime_types`), so with `DoclingProcessor` or Tesseract
enabled a tagged PNG is parsed to OCR/VLM text and lands in `dense` + `sparse`
like any PDF. (ADR-032's "the ingest scanner is PDF-only" no longer holds; this
ADR corrects it.) That path serves a photo *of a document*. It does nothing for
a photo of a coastline: OCR returns no text, so the file produces no chunks
and can never be found.

PR #800 added joint text↔image embeddings to `BedrockProvider`
(`amazon.titan-embed-image-v1`, 1024-d; `cohere.embed-v4:0`, 1536-d) and
deliberately stopped at the provider surface: `supports_image_embeddings`,
`embed_image[_batch]`, `get_image_dimension`, and `embed_for_image_space` —
a text query embedded into the *image* space, kept separate from `embed()` so
the two spaces cannot be mixed by a config typo. The indexer, the query side
and this ADR were left as follow-ups.

Constraints inherited from earlier decisions:

- **ADR-015** — features are enabled by provider capability flags, never by
  sniffing the provider class.
- **ADR-031** — one collection per tenant, named `dense` and `sparse`
  vectors; `hybrid` documents pay for dense embeddings, `keyword-index`
  documents do not; a failed embedding is a hard error routed to retry and
  dead-letter, never a silent downgrade.
- **ADR-012 / ADR-034** — retrieval methods are fused by rank (RRF/DBSF) and
  `relevance` is derived from fused rank, not raw cosine.
- **ADR-019** — every hit is verified against Nextcloud on read, keyed by
  `doc_type` + `doc_id`.

## Decision

### 1. Two paths, both run, neither chosen

Text extraction and image embedding are **complementary, not alternatives**.
An image file tagged for hybrid indexing gets:

- **text chunks** from whichever processor claims its MIME type (Docling,
  Tesseract, Mistral OCR once GH #1882 lands), stored in `dense` + `sparse`
  exactly as today. No text → no text chunks, which is correct and not an
  error; and
- **one image point** carrying only the `image` vector, when the provider
  `supports_image_embeddings`.

This removes the "which processor should an image route to" question raised on
GH #1882: there is no routing decision. A scanned receipt becomes findable by
its words *and* by its look; a landscape only by its look. The text path does
not wait on this ADR, and this ADR does not depend on any OCR backend.

### 2. Schema: a third named vector in the existing collection

```python
vectors_config={
    "dense": VectorParams(size=<text dim>,  distance=COSINE),
    "image": VectorParams(size=<image dim>, distance=COSINE),  # only when enabled
}
sparse_vectors_config={"sparse": SparseVectorParams(...)}
```

Rejected: a separate image collection. It would duplicate the tenant/alias
lifecycle, the access-control payload indexes, verify-on-read, cross-user
dedup and eviction — all of which work unchanged on a named vector, because
they key on payload, not on vector names.

Rejected: one shared multimodal space for everything (embedding text chunks
with the multimodal model too). It would tie text retrieval quality to an
image model and make swapping either model a full re-index.

### 3. Existing collections gain the vector in place (Qdrant ≥ 1.18)

`ensure_collection` (`vector/qdrant_client.py`) already validates the `dense`
dimension on startup. It is extended so that, when image embeddings are
enabled:

- the collection has no `image` vector → call
  `create_vector_name(collection, "image", DenseVectorNameConfig(...))`.
  Qdrant 1.18 added `PUT /collections/{c}/vectors/{name}`; it is idempotent
  for an identical config and is supported by qdrant-client's local mode, so
  the single-user embedded deployment needs no special case. Existing points
  are untouched — notes and files are **not** re-indexed.
- the `image` vector exists with the wrong size → refuse to start image
  indexing with the same actionable error the `dense` mismatch raises today.

(`update_collection` cannot add a vector name; earlier planning assumed it
could.)

**Image-model swap** (Titan → Cohere, 1024 → 1536) no longer needs a
`_v2` collection and alias flip: `delete_vector_name("image")`, then
`create_vector_name` at the new size, then re-enqueue image files. Text and
sparse vectors are never touched. The alias flip remains the tool for a
*text*-model change, as before.

Minimum versions: Qdrant server **1.18** for the in-place add (new collections
created with `image` work on any supported version), and `qdrant-client`
raised from `>=1.17.0` to the first release exposing `create_vector_name`.
Against an older server the call fails; the server logs an error and leaves
image indexing disabled rather than refusing to boot — text search must not
regress because an optional feature can't be provisioned.

### 4. Image points belong to the file, not a new doc type

The image point is a point of the **same file document**: `doc_type="file"`,
the file's `doc_id`, and a payload marker `vector_kind="image"` (text points
are implicitly `"text"`). Its id is `point_id(tenant, doc_id, <reserved image
chunk index>)` so it is re-written, not duplicated, on re-index.

Consequences of keeping `doc_type="file"`:

- verify-on-read, per-user path payloads (ADR-033), eviction by `doc_id` and
  the `path_prefixes` filter work with no new code;
- text-only consumers must skip `vector_kind="image"` points: context
  expansion, `exact` search, chunk counts in `nc_get_vector_sync_status`, and
  `/api/v1/chunk-context`.

Card #103 proposed `doc_type="image"`. Rejected because every per-type code
path — verification, deep links, the Astrolabe result renderer — would need a
new branch for what is, to Nextcloud, just a file.

### 5. Ingest

- **Discovery.** When image embeddings are enabled, the provider's supported
  image MIME types are added to `indexable_mime_types`, so tagged images are
  discovered even if no text processor claims them. Discovery stays
  tag-driven (`vector-index` / `keyword-index`); no walking of the whole
  Photos tree.
- **Index mode.** Only `hybrid` documents get an image vector. An image
  embedding is a paid embedding call, and `keyword-index` exists precisely to
  opt out of those (ADR-031).
- **Processing.** The worker embeds the image (downscaled to the model's
  input limits first) and upserts the image point alongside whatever text
  chunks the processor produced. "No processor claims this type" is no longer
  a failure for an image when the image point succeeded.
- **Failures.** A failed image embedding is a hard error into the existing
  retry → dead-letter path, as for dense text (ADR-031). It is not swallowed
  to "index the text and move on", which would leave a document that looks
  indexed but can never match visually.

### 6. Query: an extra prefetch, with a score floor

`bm25_hybrid` already fuses a `dense` and a `sparse` prefetch. When the
collection has an `image` vector **and** the provider supports image
embeddings, `hybrid` and `semantic` searches add a third prefetch on `image`,
using `embed_for_image_space(query)`. `bm25` and `exact` are unaffected.
Points lacking a vector simply don't appear in that prefetch's rank list, so
RRF merges text-only and image-only points without reweighting.

**The image prefetch carries a `score_threshold`.** Rank fusion cannot
abstain: without a floor, the top-k images enter the fused list for *every*
query, including "Q3 budget spreadsheet", and RRF gives the best of them a
rank comparable to a genuine text hit. Cosine *is* meaningful within one
model's space, and PR #800's eval gives a usable window: matches ≥ 0.40 /
distractors ≤ 0.18 (Titan), matches ≥ 0.38 / distractors ≤ 0.11 (Cohere v4).
The floor is a per-model default (initially 0.25 for both), overridable via
`IMAGE_SEARCH_SCORE_THRESHOLD`. It must be re-validated on a larger probe set
before this ADR is accepted.

The second embedding call is the only added query cost; it runs concurrently
with the text embedding (`anyio` task group). No new MCP tool in the first
iteration: `nc_semantic_search` becomes cross-modal when image vectors exist,
and its docstring says so. An image-only mode is deferred until someone asks
for it.

### 7. Configuration and advertisement

- Enabled when the configured provider reports `supports_image_embeddings`
  (`BEDROCK_IMAGE_EMBEDDING_MODEL` set). Absent → no `image` vector is
  created and behaviour is identical to today.
- No new `supported_search_types` entry: image retrieval is a property of
  `hybrid`/`semantic`, not a fifth algorithm. `/api/v1/status` gains an
  `image_search: bool` so Astrolabe can render image results (thumbnail via
  `/core/preview`, deep link via `nc_webdav_view_image`) only when the server
  produces them.

## Consequences

- **Bedrock-only at first.** Astrolabe Cloud embeds through the embedding
  gateway, which has no image endpoint, so the cloud does not get image search
  from this ADR alone. Adding `/v1/embeddings` image input to the gateway,
  with a consumer pact on this side, is a separate card. Ollama, OpenAI and
  Mistral providers keep the default `NotImplementedError`; users whose only
  provider is Mistral (the GH #1882 reporter) get document images through the
  OCR text path, not this one.
- **API surface.** The `/api/v1/status` field and the image results returned
  by `/api/v1/search` change a contract Astrolabe consumes: the Astrolabe
  consumer pact and this repo's provider verification must cover them, and an
  integration test must index and retrieve an image end-to-end. CI has no
  Bedrock credentials, so `SimpleProvider` gains a deterministic
  `embed_image` (hash-seeded vector) to make that test hermetic.
- **Storage.** One extra vector per indexed image (4 KiB at 1024-d float32,
  6 KiB at 1536-d), plus an HNSW graph for the `image` vector.
- **Billing.** Image embedding calls are metered separately from
  `tokens_embedded` (images are priced per image, not per token).
- **Docs.** `docs/configuration.md` (new env vars), the
  `nc_semantic_search` docstring, and a correction to ADR-032's "PDF-only"
  sentence.

## Implementation order

1. Rebase PR #800; add `SimpleProvider.embed_image`.
2. Schema: create/extend the `image` vector, dimension validation, version
   floor.
3. Ingest: discovery, image point, failure semantics, sync-status counts.
4. Query: image prefetch with score floor, `image_search` status field,
   pacts + integration test.
5. Separately: embedding-gateway image input (cloud).
