# ADR-042: Image embeddings as a third named vector

## Status

Proposed — 2026-11-22. Tracked on Deck board 12, card #103 (all card numbers
in this ADR are Deck cards, not GitHub issues). Builds on the
provider surface first prototyped in PR #800 (GH #703). The embedding-gateway
side is Deck board 10, card #1537 (astrolabe-cloud-website#1247).

**Acceptance criterion:** before this ADR moves to Accepted, the per-model
image score floors in §6 must be re-validated on a probe set of at least 50
images and 100 queries (matching, near-miss and distractor queries), and the
measured floors recorded here. Until then the implementation ships with no
default floors: image search is enabled only when the operator sets
`IMAGE_SEARCH_SCORE_THRESHOLD` (the unknown-model rule in §7 applies to every
model), so unvalidated defaults cannot ship.

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

Bedrock is one source of such models, not the design's subject. Joint
text↔image embedding models are also available through OpenRouter and as
self-hosted models behind OpenAI-compatible servers, and the embedding gateway
can route to any of them by model id. So this ADR specifies a
**provider-agnostic** capability. A provider either implements it, or reports
it as unsupported and the feature stays off. Configuration names no vendor.

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
- **one image point** carrying only the `image` vector, when the configured
  provider supports image embeddings (§7).

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

**Image-model swap** (for example 1024-d → 1536-d) no longer needs a
`_v2` collection and alias flip: `delete_vector_name("image")`, then
`create_vector_name` at the new size, then re-enqueue image files. Re-queueing
is not manual. When image embeddings are enabled, the scanner's freshness
check (`find_indexed_content`, which already compares `etag` and
`embedding_identity`) also compares `image_embedding_identity` with the
configured image model. An image whose `etag` is unchanged but whose identity
differs therefore reads as stale and is re-queued. Dead-lettered documents
retry because the model id is part of `tiers_sig`. Text and
sparse vectors are never touched.

**The identity rule, so that the check cannot loop.** The
`image_embedding_identity` comparison applies only to documents that are
(a) an image MIME type the configured model accepts, (b) `hybrid`, and
(c) in a collection with `image_vector_ready`. Every point such a document
writes carries `image_embedding_identity` set to the configured model,
whether the image point was written or **skipped** (permanent rejection or
size cap, §5). A skipped document records the skip next to the identity,
not instead of it. So a skip reads as fresh until the content or the model
changes. `keyword-index` documents, other MIME types and collections without
the vector never take part in the comparison.

**When the feature is first enabled**, existing hybrid image documents carry
no `image_embedding_identity`. Each one is re-queued **once**, to backfill
its image point. That is intended, and it is the same path as a model swap.
The volume shows in sync status as pending images before it is spent, and it
is bounded by queue concurrency. The alias flip remains the tool for a
*text*-model change, as before.

**Leaving the feature's scope removes or ignores the image point**, so no
orphan stays searchable:

- **Mode change to `keyword-index`.** The re-index of a document that is now
  `keyword-index` deletes its image point (`image_point_id` is deterministic,
  so this is one targeted delete). `prune_stale_chunks` cannot do it, because
  the image point has no `chunk_index`. Keyword documents opt out of paid
  embeddings and of dense search (ADR-031), so they must not keep an image
  vector either.
- **Feature turned off** (`IMAGE_EMBEDDING_MODEL` unset). This is a no-op for
  the data: `image_vector_ready` is false, so no image prefetch is issued and
  the points are inert. Turning the feature back on with the same model
  reuses them, and the identity rule re-queues them if the model changed.
  Purging is an explicit operator action (`delete_vector_name("image")`),
  never a side effect of configuration.

Minimum versions: Qdrant server **1.18** for the in-place add. New
collections created with `image` work on any supported version. The dev and
CI stack runs `qdrant/qdrant:v1.20.1` (`docker-compose.yml`), so the
integration lanes exercise the in-place path. `qdrant-client` is
raised from `>=1.17.0` to `>=1.18.0`, the first release that exposes
`create_vector_name` and `models.DenseVectorNameConfig` (verified: 1.17.1 has
neither, 1.18.0 has both). The client bump is **unconditional** (no guarded
import) and lands in the same implementation PR as the schema step. The
degrade path below covers an old *server*, not an old client.
Against an older Qdrant server the call fails. The MCP server then logs an
ERROR that names the restart needed to retry, and leaves image indexing
disabled for that collection rather than refusing to boot — text search must not
regress because an optional feature can't be provisioned.

### 4. Image points belong to the file, not a new doc type

The image point is a point of the **same file document**: `doc_type="file"`,
the file's `doc_id`, and a payload marker `vector_kind="image"` (text points
are implicitly `"text"`).

**It has no `chunk_index`, and its id comes from a separate name.** It is
`uuid5(NAMESPACE_DNS, "file:<doc_id>:image")`, built by
`payload_keys.image_point_id()`: the same scheme as the chunk ids the ingest
worker writes (`"file:<doc_id>:chunk:<i>"`), so it is independent of the
collection name, like them. (An earlier draft keyed it on a tenant id, which
the worker's point ids don't use.) The id is deterministic, so a re-index
overwrites the point in place. It cannot collide with a chunk id, because no
chunk name ends in `:image`. A reserved chunk index was rejected for two reasons:

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
(ADR-033), identical to the text points. Concretely, it is built from the same
document-level payload builder as the text points, so any document-level
field a reader consumes now or later (timestamps, content hash, and so on) is
present. Only the chunk-specific fields (`chunk_index`, chunk text and
offsets, `total_chunks`) are absent. The image model is recorded
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
  and the image prefetch is the only caller that drops that one condition.
  It still applies every other condition from the helper, including the
  ownership/ACL and path filters, so image results are access-controlled
  exactly like text results. A `must_not` on a
  `match` condition keeps points that lack the field, so text points (which
  have no `vector_kind`) pass through it. A test pins this against both a
  real Qdrant and the qdrant-client local-mode backend, because the two have
  differed on filter semantics before.

  Text consumers that build their own filters go through the same helper.
  The implementation PR audits every `scroll`/`count`/`query_points` call in
  `vector/` and `search/`, which today includes chunk-context, the
  `total_chunks` and sync-status counts, and the `/api/v1/vector-sync/status`
  endpoint. A test then asserts that each call site's filter carries the
  condition. It finds the call sites by walking the AST of `vector/` and
  `search/` for Qdrant client calls, not from a hand-maintained list, so a
  new call site that skips the helper fails the test. The walk fails
  closed: a Qdrant call it cannot attribute to a filter, such as one made
  through an unrecognised wrapper, fails the test rather than being skipped.
  Document-scoped operations (eviction, prune, ACL release) are excluded
  through an explicit allowlist, because they must reach the image point.

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
- **Input limits.** Every image-capable provider exposes
  `image_input_limits` **for the configured model**. That covers accepted
  MIME types, max bytes per image, max pixels, and max images per request.
  Each provider fills it from the most authoritative source it has (§8): the
  gateway reads `image_mime_types` from `/v1/models` and applies its request
  caps of 5 MiB decoded per image and 16 images per request; Bedrock uses a
  static per-model table. Those MIME types bound what discovery adds (see
  **Formats**). The worker downscales an image that exceeds the pixel or
  byte limit before embedding, and the provider chunks batches to the
  per-request cap. A file above `IMAGE_EMBEDDING_MAX_SOURCE_BYTES` (default
  50 MB) is not embedded at all: it is recorded as skipped, not failed, and
  counted in sync status. A size-cap skip is recorded exactly like a
  permanent rejection (below): `image_skipped_reason` on the text points if
  there are any, otherwise the terminal dead-letter marker. A document with
  no points and no record would re-queue every cycle.
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
  points of one document succeed or fail together. A bounded-retry fallback
  to text-only (`image_skipped_reason="provider_unavailable"`) was rejected:
  it would mark documents indexed without their image vector during an
  outage, and nothing would re-queue them afterwards. A long outage instead
  shows up as a growing retry and dead-letter count, which is visible and
  self-heals once the dead-letter markers are cleared. A transient failure
  (timeout, 5xx, 429) retries as usual. A **permanent** rejection of the
  input (classified as below, for example a corrupt file or an unsupported
  encoding) is not retried. The image point
  is recorded as skipped with the reason, and the document's text chunks are
  written. Retrying the same bytes cannot succeed, so dead-lettering them
  would only add noise.

  **Classification is by error type, never by status code alone.** Each
  provider maps its own errors to one provider-agnostic exception,
  `ImageInputRejected(reason)`, which is the only signal the worker treats
  as permanent. Anything else is transient and retries. A misclassified
  transient error would stay stuck until the content or the model changed,
  so a provider raises it only for a response that names the input as
  unusable. Each provider has a unit test pinning its full classification
  table, not just the resulting behaviour:

  | Provider | Permanent (`ImageInputRejected`) | Retried |
  |---|---|---|
  | Gateway | 422 with `detail.code` = `invalid_image`, `unsupported_mime_type` | `image_too_large`, `too_many_images` (client bugs, logged at ERROR), list-shaped 422, other 4xx, 429, 5xx, timeouts |
  | Bedrock | `ValidationException` naming the input | bare 400, `ThrottlingException`, 429, 5xx, timeouts |

  The gateway puts `{"code", "index", "message"}` in a dict-shaped `detail`
  on every image 422. Other 422s keep FastAPI's list shape. The provider
  branches on `isinstance(detail, dict) and "code" in detail` and never
  parses `message`. `image_too_large` and `too_many_images` mean the client
  skipped downscaling or chunking. They must never be recorded as a skip
  against the image. A rejection of the configuration (the gateway's 400 for
  an image sent to a text-only model) would hit every image, so it fails
  loudly at startup through the capability check, not per document.

  The skip is recorded so that the scanner honours it:
  - **with text chunks**, the text points carry `image_skipped_reason`. The
    document reads as indexed (it is, as text), so the scanner does not
    re-queue it. Sync status counts it as skipped;
  - **with no text chunks**, the document has no points. Without a record the
    scanner would re-queue it every cycle, the loop §4 avoids. So the worker
    writes the existing terminal dead-letter marker (`mark_dead_letter`,
    `vector/dead_letter.py`). That marker is content-addressed on `etag` plus
    `tiers_sig`, and the scanner honours it for every user. The image model id
    is part of `tiers_sig` **for image MIME types only**, so swapping the
    image model never re-queues PDFs or other documents. New content or a
    model change makes the
    document retryable again.
- **Formats.** `indexable_mime_types` gains the intersection of the
  configured model's accepted types and what the worker can decode for
  downscaling. Accepted types differ per model, which is why the list is read
  from the provider and never hardcoded. Titan Multimodal takes only PNG and
  JPEG, while Cohere v4 also takes WebP and GIF. An `unsupported_mime_type`
  skip therefore applies to the current model only: the model id is in
  `tiers_sig`, so a model change retries it. An animated GIF or WebP is
  embedded from its first frame. HEIC/HEIF, which phones produce by default,
  is **out of scope** for now: none of the models above accepts it, and
  converting it would need a new decoder dependency.
- **Cost.** The first sync of a large tagged image tree issues one paid call
  per image. Spend is bounded by the existing ingest queue concurrency and the
  source-size cap above. `nc_get_vector_sync_status` reports pending,
  embedded and skipped image counts, so an operator can see the volume before
  it is spent, by tagging a folder and reading the pending count. A hard
  spend ceiling is out of scope here. Behind the gateway, spend is metered
  gateway-side as `images_embedded` (#1537).

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
on the image model id **and output width**: `IMAGE_EMBEDDING_DIMENSIONS`
truncation changes the score distribution, so a floor holds only at the width
it was measured at, and the probe set records that width. The model part of the
key is the model's own id with any gateway routing
namespace stripped (`bedrock/amazon.titan-embed-image-v1` and
`amazon.titan-embed-image-v1` share a floor), because the score scale belongs
to the model, not to the provider that serves it. The provisional values are the midpoints of those
windows, about 0.29 for Titan and 0.25 for Cohere v4. They are overridable via
`IMAGE_SEARCH_SCORE_THRESHOLD`. Three probes on a single set are not evidence,
so the acceptance criterion in Status gates them. They are candidates, not
shipped defaults, until it is met.

The two failure directions are not symmetric. A floor that is too low lets
irrelevant images into the results, which is visible. A floor that is **too
high** silently removes every image-only file from all results, which nobody
notices. Short or abstract queries ("summer", "that thing from Tuesday") also
score low against every image. Here the floor is doing its job, but the
effect looks the same. To keep a too-high floor from going unnoticed, the
search path emits a metric with the number of image-prefetch hits above the
floor per query. A floor that suppresses everything then shows up as a flat
zero on a dashboard. Sync status and a gauge report the number of indexed
image points next to it. That separates "the floor is too high" (images
indexed, zero hits) from "no images indexed" (both zero).

The threshold behaviour is tested against both a real Qdrant and local
mode, like the `must_not` semantics in §4.

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

- **Provider-agnostic settings.** The image model is served by the same
  provider the registry selected for text embeddings: one provider per
  deployment, as today.

  | Setting | Meaning |
  |---|---|
  | `IMAGE_EMBEDDING_MODEL` | Image model id on the active provider. Unset (default) means image embeddings are off. |
  | `IMAGE_EMBEDDING_DIMENSIONS` | Optional output width for models that support truncation. Mirrors `EMBEDDING_DIMENSIONS`. |
  | `IMAGE_EMBEDDING_MAX_SOURCE_BYTES` | Files above this are skipped, not embedded (default 50 MB). |
  | `IMAGE_SEARCH_SCORE_THRESHOLD` | Floor for the image prefetch (§6). **Required** until the Status acceptance criterion is met: without it the feature stays off, with a startup warning. `docs/configuration.md` states this at the top of the image section, so an unset threshold doesn't read as a broken feature. |

  An operator who wants text and images from different upstreams (say,
  Mistral text and a Bedrock image model) points both at the gateway, which
  routes per model id. A second provider slot is not added.
- **Enabled only on confirmation, never on configuration alone.** Image
  embeddings are on when `IMAGE_EMBEDDING_MODEL` is set **and** the provider
  confirms that the model takes images (§8). A model that is set but not
  confirmed logs an ERROR at startup and leaves the feature off. Unset means
  no `image` vector is created and behaviour is identical to today.
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
  flag is decided once per collection, when `ensure_collection` first runs
  for it: at startup for collections that already exist, and on first use
  for a tenant collection created later. A collection whose
  `create_vector_name` failed stays text-only until the next restart retries
  it. This also means
  `/api/v1/status.image_search` does not change between restarts, so
  Astrolabe may cache it.
- **A floor is required.** Until the Status acceptance criterion is met,
  **every** model takes the path described next, because no default floors
  ship. After acceptance, the validated per-model floors from §6 apply to
  the known model ids. For any other image model, `image_vector_ready` stays false
  unless the operator sets `IMAGE_SEARCH_SCORE_THRESHOLD` explicitly, and a
  startup warning says so. Running without a floor would bring back the
  RRF cannot-abstain problem, so it is never the silent default.
- No new `supported_search_types` entry: image retrieval is a property of
  `hybrid`, not a fifth algorithm. `/api/v1/status` gains
  `image_search: bool`, which reports `image_vector_ready`, the effective
  capability, not the provider flag. Astrolabe renders image results
  (thumbnail via `/core/preview`, deep link via `nc_webdav_view_image`) only
  when it is true.

### 8. Provider contract

The `Provider` base class gains one optional capability. Every member
defaults to "unsupported", so providers without image support need no
changes:

- `supports_image_embeddings` — true only after confirmation (below);
- `get_image_dimension()`;
- `image_input_limits` — MIME types, max bytes, max pixels, max images per
  request (§5);
- `embed_image_batch(images)`, which chunks to the per-request cap itself;
- `embed_for_image_space(query)` — a text query embedded into the image
  model's space;
- errors that name the input as unusable raise `ImageInputRejected` (§5).

How each provider confirms the capability and learns its limits:

| Provider | Confirmation and limits | Status |
|---|---|---|
| Embedding gateway | `/v1/models`: `"image"` in `input_modalities`, plus `dimension` and `image_mime_types` on the model entry. Caps of 5 MiB and 16 images per request. Wire format: image items in `/v1/embeddings` `input`, `{"type": "image", "data": <base64>, "mime_type": ...}`. A plain string sent to the image model id is embedded into the image space. Consumer pact against provider state `"the gateway serves an image embedding model"`. | First implementation. Gateway side in astrolabe-cloud-website#1247. |
| Bedrock | Static per-model table for known image models (Titan Multimodal, Cohere v4): dimension, MIME types, limits. An unknown id is not confirmed. Cohere v4 is asymmetric: images are sent as `input_type=search_document` and `embed_for_image_space` queries as `search_query`. Titan has no input type. | From the PR #800 prototype. |
| OpenAI-compatible (`OpenAIProvider` with `OPENAI_BASE_URL`: OpenRouter, self-hosted servers) | No standard wire format or advertisement for image embeddings exists across these endpoints. Support is added per target once its format is pinned by an integration test, with confirmation by a one-image probe at startup. Until then the capability stays off. Through the gateway these models are already reachable, with one wire format. | Follow-up. |
| Ollama, Mistral | No image embedding support today; capability off. | — |
| `SimpleProvider` | Deterministic hash-seeded image vectors. Confirmed **only** for the explicit test sentinel `IMAGE_EMBEDDING_MODEL=simple-image-hash`, because `SimpleProvider` is also the autodetect fallback: a production deployment with a typo in its provider config must not index meaningless image vectors and look as if it works. Makes CI's e2e test hermetic without credentials. | With the base class. |

The gateway row is the recommended route for anything beyond Bedrock. It
gives one wire format, model advertisement and metering for every upstream
the gateway can reach.

## Consequences

- **Provider coverage at first ship** is the gateway and Bedrock (§8), plus
  `SimpleProvider` for tests. Astrolabe Cloud gets image search through the
  gateway. Self-hosted deployments get it through Bedrock, or through any
  model a gateway routes to. Direct OpenAI-compatible support (OpenRouter,
  self-hosted servers) follows per target. A user whose only provider is
  Mistral (the GH #1882 reporter) gets document images through the OCR text
  path, not this one.
- **API surface.** The `/api/v1/status` field and the image results returned
  by `/api/v1/search` change a contract Astrolabe consumes: the Astrolabe
  consumer pact and this repo's provider verification must cover them, and an
  integration test must index and retrieve an image end-to-end. CI has no
  provider credentials, so `SimpleProvider` gains a deterministic
  `embed_image` (hash-seeded vector) to make that test hermetic. It runs with
  `IMAGE_EMBEDDING_MODEL=simple-image-hash` and
  `IMAGE_SEARCH_SCORE_THRESHOLD=0`: hash vectors carry no meaning, so the e2e
  test asserts the plumbing (the image point is written, and returned through
  the prefetch with ACLs applied), not relevance. Relevance and the floors
  belong to the eval-tier probe set (§6). A list-shaped gateway 422 on an
  image request is pinned as *retried* by a unit test of the classifier. The
  pact covers the dict-shaped codes the gateway produces for image items. Remaining
  gap: provider verification covers only the public endpoints, so
  `image_search` on `/api/v1/status` is verified but image results on the
  authenticated `/api/v1/search` are not. That is the existing ADR-029
  phase-4 gap, tracked on board 11. The implementation PR adds image
  results to that card instead of leaving them silently uncovered.
- **Data egress.** Image bytes are sent to the configured embedding
  upstream (Bedrock, the gateway and whatever it routes to, or a future
  OpenAI-compatible endpoint). Indexing is opt-in per folder through tags,
  but operators with privacy requirements should know that images leave the
  deployment unless the provider is self-hosted. `docs/configuration.md`
  says so next to `IMAGE_EMBEDDING_MODEL`.
- **Storage.** One extra vector per indexed image (4 KiB at 1024-d float32,
  6 KiB at 1536-d), plus an HNSW graph for the `image` vector.
- **Billing.** Image embedding calls are metered separately from
  `tokens_embedded` (images are priced per image, not per token). A model
  swap (§3) is cheap in schema terms but **re-bills every indexed image**:
  it is a full image re-embed.
- **Docs.** `docs/configuration.md` (new env vars:
  `IMAGE_EMBEDDING_MODEL`, `IMAGE_EMBEDDING_DIMENSIONS`,
  `IMAGE_EMBEDDING_MAX_SOURCE_BYTES`, `IMAGE_SEARCH_SCORE_THRESHOLD`; PR #800's
  `BEDROCK_IMAGE_EMBEDDING_MODEL` / `BEDROCK_IMAGE_OUTPUT_DIM` never shipped
  and are replaced, not deprecated), the
  `nc_semantic_search` docstring, and a correction to ADR-032's "PDF-only"
  sentence.

## Implementation order

1. Provider-agnostic surface: the base-class capability (§8),
   `ImageInputRejected`, the `IMAGE_EMBEDDING_*` settings and capability
   confirmation, `SimpleProvider.embed_image`, and Bedrock from the PR #800
   prototype (superseding #800).
2. `GatewayProvider` image support: confirmation from `/v1/models`, chunking
   to 16, the 422-code classification, and the consumer pact (gateway side:
   #1247).
3. Schema: create or extend the `image` vector, per-collection validation,
   `image_vector_ready`, version floor.
   - The startup log lists **every** reason `image_vector_ready` is false for
     a collection (old Qdrant, size mismatch, model not confirmed, no floor
     configured).
4. Ingest: discovery, `image_point_id`, single-write failure semantics,
   central `vector_kind` exclusion, sync-status counts.
   - Dual-backend test: a range filter skips points without `chunk_index`,
     so prune leaves the image point alone.
   - `tiers_sig` is made mode-aware if it isn't already (`index_mode` joins
     the image-type signature), so a marker written under one mode never
     suppresses a retry under the other.
   - The identity rule from §3, with tests for a skipped image, a
     `keyword-index` image and first-enable backfill.
5. Query: image prefetch with per-model floor and hit metric, `image_search`
   status field, Astrolabe pact and integration test.
   - The hit metric is labelled by image model only, never per query or per
     user.
   - With `fusion="dbsf"` the image prefetch is skipped, and a debug log
     says so.
6. Follow-ups: direct OpenAI-compatible targets (OpenRouter, self-hosted),
   each once its wire format is pinned.

Each step ships its own tests in the same PR, per the repo's API-surface
rule: unit tests for every step, an integration (e2e) test for steps 3, 4
and 5, and Pact coverage for steps 2 and 5. Step 3's integration test runs
the in-place add against the CI Qdrant (`v1.20.1`). The old-Qdrant degrade
path is covered by a unit test that fails `create_vector_name`, since CI does
not run an old server.
