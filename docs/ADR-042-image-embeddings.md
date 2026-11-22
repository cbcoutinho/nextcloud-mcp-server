# ADR-042: Image embeddings as a third named vector

## Status

Proposed — 2026-11-22. Tracked on Deck board 12, card #103. Builds on the
provider surface in PR #800 (GH #703). The embedding-gateway side is Deck
board 10, card #1537.

**Acceptance criterion:** before this ADR moves to Accepted, the per-model
image score floors in §6 must be re-validated on a probe set of at least 50
images and 100 queries (matching, near-miss and distractor queries), and the
measured floors recorded here.

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
raised from `>=1.17.0` to `>=1.18.0`, the first release that exposes
`create_vector_name` and `models.DenseVectorNameConfig` (verified: 1.17.1 has
neither, 1.18.0 has both). The client bump is **unconditional** (no guarded
import) and lands in the same implementation PR as the schema step. The
degrade path below covers an old *server*, not an old client.
Against an older server the call fails; the server logs an error and leaves
image indexing disabled rather than refusing to boot — text search must not
regress because an optional feature can't be provisioned.

### 4. Image points belong to the file, not a new doc type

The image point is a point of the **same file document**: `doc_type="file"`,
the file's `doc_id`, and a payload marker `vector_kind="image"` (text points
are implicitly `"text"`).

**It has no `chunk_index`, and its id comes from a separate name.** It is
`uuid5(NAMESPACE, canonical_json({"tenant_id", "doc_id", "vector_kind":
"image"}))`, built by a new `payload_keys.image_point_id()` next to
`point_id()`. The id is deterministic, so a re-index overwrites the point in
place. It cannot collide with a chunk id, because the canonical JSON has a
different key set. A reserved chunk index was rejected for two reasons:

- `prune_stale_chunks` deletes every point of the document with
  `chunk_index >= kept_chunks` after each re-index. A high sentinel would be
  deleted on every run.
- A `-1` sentinel would leak into code that assumes indices are non-negative
  (context expansion, ordering, `total_chunks`).

A Qdrant range filter does not match a point that lacks the field, so prune
leaves the image point alone. Whole-document deletion (eviction,
`delete_document_points`) filters on `doc_id`/`doc_type`, so the image point
shares the file's lifecycle. A unit test pins both behaviours. Nothing counts
the image point as a chunk: `total_chunks` is `len(chunks)` of the text
split, and prune selects by `chunk_index` range. The audit below checks every
other per-document point count for double counting.

**The image point carries the full document payload.** That means `etag`,
`index_mode`, `embedding_identity`, `acl_principals` and the per-user paths
(ADR-033), identical to the text points. The image model is recorded
separately as `image_embedding_identity`. This is required, not cosmetic:
`find_indexed_content` and `query_document_metadata` answer "is this document
already indexed?" from an **unordered** `scroll(limit=1)` over the document's
points. An image point with a partial payload would read as "not indexed"
whenever that scroll returned it. The scanner would then re-queue the
document on every cycle, the non-converging loop `prune_stale_chunks` exists
to close (Deck #1084). Cross-user sharing and release
(`release_document_for_user`) select by `doc_id`, so they update the image
point together with the text points.

Consequences of keeping `doc_type="file"`:

- verify-on-read, per-user path payloads (ADR-033), eviction by `doc_id` and
  the `path_prefixes` filter work with no new code;
- text-only consumers must skip image points: context expansion, `exact`
  search, chunk counts in `nc_get_vector_sync_status`, and
  `/api/v1/chunk-context`. A missed skip would silently return an image as an
  empty-text chunk. So the exclusion (`must_not: vector_kind == "image"`) is
  added **centrally**, in `search/access_filter.build_base_filter_conditions`,
  and the image prefetch is the only caller that opts out. A `must_not` on a
  `match` condition keeps points that lack the field, so text points (which
  have no `vector_kind`) pass through it. A test pins this against both a
  real Qdrant and the qdrant-client local-mode backend, because the two have
  differed on filter semantics before.

  Text consumers that build their own filters go through the same helper.
  The implementation PR audits every `scroll`/`count`/`query_points` call in
  `vector/` and `search/`, which today includes chunk-context, the
  `total_chunks` and sync-status counts, and the `/api/v1/vector-sync/status`
  endpoint. A test then asserts that each call site's filter carries the
  condition. Document-scoped operations (eviction, prune, ACL release) are
  deliberately excluded, because they must reach the image point.

Card #103 (board 12) proposed `doc_type="image"`. Rejected because every per-type code
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
- **Input limits.** The provider exposes `image_input_limits` (accepted MIME
  types, max bytes, max pixels), which is added to the PR #800 surface. Those
  MIME types are what discovery adds to `indexable_mime_types`. The gateway
  accepts `image/jpeg`, `image/png`, `image/gif` and `image/webp` (#1537).
  The worker downscales an image that exceeds the pixel or byte limit before
  embedding. A file above `IMAGE_EMBED_MAX_SOURCE_BYTES` (default 50 MB) is
  not embedded at all: it is recorded as skipped, not failed, and counted in
  sync status.
- **Processing, one write.** For one document job the worker extracts text
  and computes every embedding (text chunks and image) first. It then writes
  them in a single upsert, followed by `prune_stale_chunks`. "No processor
  claims this type" is not a failure for an image when the image embedding
  succeeds.
- **Failures and retries.** Any embedding failure (text or image) fails the
  job before the upsert, so nothing partial is written. The job then goes
  through the existing retry → dead-letter path (ADR-031). An image embedding
  that succeeds while the text path fails therefore writes neither, and the
  same holds the other way round. Because both kinds of point id are
  deterministic, a retry's upsert is idempotent. An image with no text
  processor whose image embedding fails dead-letters like any other failed
  document. It is never recorded as "indexed with nothing", which would look
  indexed while being unfindable.

  The trade-off is accepted on purpose: while the image provider is down,
  tagged images are not text-indexed either, because the text and image
  points of one document succeed or fail together. A transient failure
  (timeout, 5xx, 429) retries as usual. A **permanent** rejection of the
  input (the provider or gateway answers 400/422 for that image, for example
  a corrupt file or an unsupported encoding) is not retried. The image point
  is recorded as skipped with the reason, and the document's text chunks are
  written. Retrying the same bytes cannot succeed, so dead-lettering them
  would only add noise. The skip is recorded so that the scanner honours it:
  - **with text chunks**, the text points carry `image_skipped_reason`. The
    document reads as indexed (it is, as text), so the scanner does not
    re-queue it. Sync status counts it as skipped;
  - **with no text chunks**, the document has no points. Without a record the
    scanner would re-queue it every cycle, the loop §4 avoids. So the worker
    writes the existing terminal dead-letter marker (`mark_dead_letter`,
    `vector/dead_letter.py`). That marker is content-addressed on `etag` plus
    `tiers_sig`, and the scanner honours it for every user. The image model id
    is part of `tiers_sig`, so new content or a model change makes the
    document retryable again.
- **Formats.** `indexable_mime_types` gains the intersection of the
  provider's accepted types and what the worker can decode for downscaling.
  An animated GIF or WebP is embedded from its first frame. HEIC/HEIF, which
  phones produce by default, is **out of scope** for now:
  neither Bedrock model nor the gateway accepts it, and converting it would
  need a new decoder dependency.
- **Cost.** The first sync of a large tagged image tree issues one paid call
  per image. Spend is bounded by the existing ingest queue concurrency and the
  source-size cap above. `nc_get_vector_sync_status` reports pending,
  embedded and skipped image counts, so an operator can see the volume before
  it is spent, by tagging a folder and reading the pending count. A hard
  spend ceiling is out of scope here. In the cloud the gateway meters
  `images_embedded` (#1537).

### 6. Query: an extra prefetch, with a score floor

`bm25_hybrid` already fuses a `dense` and a `sparse` prefetch. When the
collection has an `image` vector **and** the provider supports image
embeddings, `hybrid` searches add a third prefetch on `image`,
using `embed_for_image_space(query)`. `semantic`, `bm25` and `exact` are
unaffected. `semantic` is a single dense query with no fusion step today, and
giving it one only for images is not worth a new fusion path in this
iteration.
Points lacking a vector simply don't appear in that prefetch's rank list, so
RRF merges text-only and image-only points without reweighting.

**The image prefetch carries a `score_threshold`.** Rank fusion cannot
abstain: without a floor, the top-k images enter the fused list for *every*
query, including "Q3 budget spreadsheet", and RRF gives the best of them a
rank comparable to a genuine text hit. Cosine *is* meaningful within one
model's space, and PR #800's eval gives a starting window: matches ≥ 0.40 /
distractors ≤ 0.18 (Titan), matches ≥ 0.38 / distractors ≤ 0.11 (Cohere v4).
The two models' scales differ, so the floor is a **per-model** default keyed
on the image model id. The provisional values are the midpoints of those
windows, about 0.29 for Titan and 0.25 for Cohere v4. They are overridable via
`IMAGE_SEARCH_SCORE_THRESHOLD`. Three probes on a single set are not evidence,
so the acceptance criterion in Status gates them.

The two failure directions are not symmetric. A floor that is too low lets
irrelevant images into the results, which is visible. A floor that is **too
high** silently removes every image-only file from all results, which nobody
notices. Short or abstract queries ("summer", "that thing from Tuesday") also
score low against every image. Here the floor is doing its job, but the
effect looks the same. To keep a too-high floor from going unnoticed, the
search path emits a metric with the number of image-prefetch hits above the
floor per query. A floor that suppresses everything then shows up as a flat
zero on a dashboard.

**RRF only.** A prefetch `score_threshold` cuts the candidate list before
fusion. That is harmless for RRF, which uses ranks. DBSF normalises each
prefetch by its score distribution, and a list truncated at the floor skews
that normalisation. So in this iteration the image prefetch is added only
under `fusion="rrf"`, the default. With `fusion="dbsf"` the search runs
text-only, and the tool docstring says so. Making DBSF work is follow-up work,
for example normalising the image list before thresholding.

The probe set and its harness are committed under `tests/` (eval tier, not CI)
so the floors can be re-measured whenever a model changes.

The second embedding call is the only added query cost; it runs concurrently
with the text embedding (`anyio` task group). No new MCP tool in the first
iteration: `nc_semantic_search` becomes cross-modal when image vectors exist,
and its docstring says so. An image-only mode is deferred until someone asks
for it.

### 7. Configuration and advertisement

- Enabled when the configured provider reports `supports_image_embeddings`
  (`BEDROCK_IMAGE_EMBEDDING_MODEL` set). Absent → no `image` vector is
  created and behaviour is identical to today.
- **Effective capability, decided once per collection.** `ensure_collection`
  validates each collection independently: one deployment has one provider,
  so one image model, but every tenant collection is checked and, if needed,
  extended on its own. The outcome is cached per collection as
  `image_vector_ready`. It is true only when the provider supports image
  embeddings **and** the collection has an `image` vector of the right size.
  The query side adds the image prefetch only when that flag is true, so an
  older Qdrant (no `create_vector_name`) or a size mismatch never sends a
  prefetch to a vector that doesn't exist. **Ingest is gated on the same
  flag**: while it is false, no image embedding is computed or paid for. The
  flag is decided at startup only. A collection whose `create_vector_name`
  failed stays text-only until the next restart retries it.
- **A floor is required.** The per-model floors in §6 exist for the known
  model ids. For any other image model, `image_vector_ready` stays false
  unless the operator sets `IMAGE_SEARCH_SCORE_THRESHOLD` explicitly, and a
  startup warning says so. Running without a floor would bring back the
  RRF cannot-abstain problem, so it is never the silent default.
- No new `supported_search_types` entry: image retrieval is a property of
  `hybrid`, not a fifth algorithm. `/api/v1/status` gains
  `image_search: bool`, which reports `image_vector_ready`, the effective
  capability, not the provider flag. Astrolabe renders image results
  (thumbnail via `/core/preview`, deep link via `nc_webdav_view_image`) only
  when it is true.

## Consequences

- **Bedrock directly, or through the gateway.** Astrolabe Cloud embeds
  through the embedding gateway. Its image support (Deck board 10, card #1537)
  extends `POST /v1/embeddings`: an `input` item may be
  `{"type": "image", "data": <base64>, "mime_type": ...}`, and a plain string
  sent to an image model id is embedded into that image space. `GET
  /v1/models` advertises `input_modalities`. `GatewayProvider` sets
  `supports_image_embeddings` when `"image"` is in `input_modalities`, takes
  `get_image_dimension` from `dimension`, and implements
  `embed_for_image_space` as a text input to the image model. It carries a
  consumer pact against the provider state `"the gateway serves an image
  embedding model"`. Ollama, OpenAI and
  Mistral providers keep the default `NotImplementedError`; users whose only
  provider is Mistral (the GH #1882 reporter) get document images through the
  OCR text path, not this one.
- **API surface.** The `/api/v1/status` field and the image results returned
  by `/api/v1/search` change a contract Astrolabe consumes: the Astrolabe
  consumer pact and this repo's provider verification must cover them, and an
  integration test must index and retrieve an image end-to-end. CI has no
  Bedrock credentials, so `SimpleProvider` gains a deterministic
  `embed_image` (hash-seeded vector) to make that test hermetic. Remaining
  gap: provider verification covers only the public endpoints, so
  `image_search` on `/api/v1/status` is verified but image results on the
  authenticated `/api/v1/search` are not. That is the existing ADR-029
  phase-4 gap, tracked on board 11. The implementation PR adds image
  results to that card instead of leaving them silently uncovered.
- **Storage.** One extra vector per indexed image (4 KiB at 1024-d float32,
  6 KiB at 1536-d), plus an HNSW graph for the `image` vector.
- **Billing.** Image embedding calls are metered separately from
  `tokens_embedded` (images are priced per image, not per token).
- **Docs.** `docs/configuration.md` (new env vars:
  `BEDROCK_IMAGE_EMBEDDING_MODEL`, `BEDROCK_IMAGE_OUTPUT_DIM`,
  `IMAGE_EMBED_MAX_SOURCE_BYTES`, `IMAGE_SEARCH_SCORE_THRESHOLD`), the
  `nc_semantic_search` docstring, and a correction to ADR-032's "PDF-only"
  sentence.

## Implementation order

1. Provider surface: land PR #800 (rebased, or superseded if it has gone
   stale), add `image_input_limits`, and add `SimpleProvider.embed_image`.
2. Schema: create or extend the `image` vector, per-collection validation,
   `image_vector_ready`, version floor.
3. Ingest: discovery, `image_point_id`, single-write failure semantics,
   central `vector_kind` exclusion, sync-status counts.
4. Query: image prefetch with per-model floor and hit metric, `image_search`
   status field, Astrolabe pact and integration test.
5. `GatewayProvider` image methods and consumer pact (gateway side: #1537).
