# Reranking (cross-encoder)

Reranking is the single largest ordering improvement available to semantic
search here, and it is **off by default**. This page is how you turn it on,
including **without** an Astrolabe embedding gateway — the case that matters for
self-hosters, because **Ollama serves no rerank models at all**.

## What it does, and why the scores looked wrong without it

Hybrid search fuses a dense (vector) ranking and a sparse (BM25) ranking with
Reciprocal Rank Fusion. An RRF score is an artifact of **rank**, not of
relevance: at the default `VECTOR_SEARCH_RRF_K=60` the maximum achievable score
is about `0.033`, so a near-perfect hit renders as "3%" if you read it as a
percentage. It also cannot tell you a result is *bad* — every retrieved row gets
a rank, so nonsense queries still return the corpus.

A cross-encoder is different in kind. It reads the query and each candidate
**together** and emits a score computed from that pair, so it can say "none of
these answer the question". Turning it on:

- **reorders** the retrieved pool by actual query/document relevance, and
- makes the `relevance` field a **calibrated probability** rather than an
  ordinal (`relevance_source: cross_encoder_calibrated`), which is what makes a
  `min_relevance` filter behave the way you expect. See
  [ADR-034](ADR-034-relevance-score.md).

Reranking never fails a search: any error, timeout, or unconfigured endpoint
degrades to retrieval order with `reranked: false`.

> **A better embedding model is not a substitute.** Swapping embedding models
> changes which documents are *retrieved*; it does not give you a relevance
> scale. If a query for "grants" misses a document about "subsidies", raise
> recall first (retrieval), then let the reranker fix the ordering.

## Pick a backend

Any endpoint speaking the **Cohere rerank protocol** works — request
`{model, query, documents, top_n}`, response
`{"results": [{"index", "relevance_score"}]}`. That covers:

| Backend | Endpoint path | Auth | Notes |
|---|---|---|---|
| [Infinity](https://github.com/michaelfeil/infinity) | `POST /rerank` | optional `--api-key` | Self-hosted. Also serves OpenAI-compatible `/embeddings`, so **one process covers both jobs**. CPU or GPU image. |
| [vLLM](https://docs.vllm.ai) | `POST /v1/rerank` | optional API key | Self-hosted, GPU-oriented. Also serves `/rerank` and `/v2/rerank`. |
| [Cohere](https://docs.cohere.com/reference/rerank) | `POST /v2/rerank` | API key (required) | Hosted. No local GPU, per-call cost, your queries and candidate text leave your network. |
| Astrolabe embedding gateway | `POST /v1/rerank` | OIDC M2M | The managed path; configured via `EMBEDDING_GATEWAY_URL`. |

**Ollama is not on this list and cannot be.** It exposes generation and
embedding APIs only — there is no rerank endpoint to point at. If you are
running Ollama today and want reranking, run Infinity **alongside or instead of
it**; Infinity's `/embeddings` is OpenAI-compatible, so it can serve both.

Not supported: HuggingFace **TEI**'s `/rerank`, which returns `[{index, score}]`
rather than the Cohere envelope.

## Self-hosted with Infinity (recommended starting point)

An opt-in Compose profile is included:

```bash
docker compose --profile infinity up -d infinity
```

It runs `BAAI/bge-m3` (embeddings) and `BAAI/bge-reranker-v2-m3` (reranking) on
port `7997`. First boot downloads ~2.5 GB of weights into the `infinity-cache`
volume; the healthcheck has a 300s start period for that reason.

Or run it directly:

```bash
docker run -it --rm -p 7997:7997 michaelf34/infinity:0.0.77-cpu \
  v2 --model-id BAAI/bge-m3 --model-id BAAI/bge-reranker-v2-m3 --port 7997
```

Then point the MCP server at it:

```bash
SEARCH_RERANK_ENABLED=true
SEARCH_RERANK_URL=http://infinity:7997/rerank
SEARCH_RERANK_MODEL=BAAI/bge-reranker-v2-m3   # bare id — no gateway prefix

# Embeddings from the same process (OpenAI-compatible surface)
OPENAI_BASE_URL=http://infinity:7997
OPENAI_API_KEY=dummy                          # unused by Infinity; its presence selects the OpenAI provider
OPENAI_EMBEDDING_MODEL=BAAI/bge-m3
```

`OPENAI_API_KEY` is what the provider registry keys the OpenAI-compatible path
on ([ADR-015](ADR-015-unified-provider-architecture.md)), so it must be set to
something even when Infinity ignores it. If you started Infinity with
`--api-key`, use that value for both `OPENAI_API_KEY` and
`SEARCH_RERANK_API_KEY`.

**CPU reranking is slow.** A 200-candidate pool on a CPU box takes tens of
seconds. Either use the GPU image (drop the `-cpu` suffix, add
`--gpus all`) or trade depth for latency:

```bash
SEARCH_RERANK_POOL_SIZE=50
SEARCH_RERANK_TIMEOUT_SECONDS=60
```

## Self-hosted with vLLM

```bash
vllm serve BAAI/bge-reranker-v2-m3 --runner pooling --port 8000
```

```bash
SEARCH_RERANK_ENABLED=true
SEARCH_RERANK_URL=http://vllm:8000/v1/rerank
SEARCH_RERANK_MODEL=BAAI/bge-reranker-v2-m3
```

## Hosted with Cohere

```bash
SEARCH_RERANK_ENABLED=true
SEARCH_RERANK_URL=https://api.cohere.com/v2/rerank
SEARCH_RERANK_MODEL=rerank-v3.5
SEARCH_RERANK_API_KEY=<your Cohere API key>
```

Note what this sends off-network: the query, plus up to
`SEARCH_RERANK_POOL_SIZE` candidate excerpts (truncated to 2000 characters
each), on **every reranked search**.

## Verify it

`GET /api/v1/status` advertises the capability, so check that first — it tells
you whether the server thinks it is configured, before you debug the reranker:

```bash
curl -s localhost:8000/api/v1/status | jq .rerank_available   # expect: true
```

Then run a search with `rerank: true` and read the response:

- `reranked: true` — it ran.
- `reranked: false` with the flag requested — it was attempted and **degraded**.
  Check the server log for `rerank unavailable, using retrieval order: …`.
- `relevance_source: cross_encoder_calibrated` — the `relevance` values are
  calibrated probabilities you may render as percentages.

Note that after a failure the stage enters a **30-second cooldown** and skips
reranking outright, so a fix will not appear to take effect instantly.

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `SEARCH_RERANK_ENABLED` | `false` | Master switch and capability gate. Requires `SEARCH_RERANK_URL` **or** `EMBEDDING_GATEWAY_URL`, else startup fails. |
| `SEARCH_RERANK_URL` | - | Full rerank endpoint URL, path included. Unset = `<EMBEDDING_GATEWAY_URL>/v1/rerank`. |
| `SEARCH_RERANK_API_KEY` | - | Static bearer for that URL. Wins over gateway M2M credentials. |
| `SEARCH_RERANK_MODEL` | `local/BAAI/bge-reranker-v2-m3` | Model id **as the endpoint expects it**: `<provider>/`-prefixed for the gateway, bare for a direct URL. |
| `SEARCH_RERANK_POOL_SIZE` | `200` | Candidates handed to the reranker. It can only reorder what retrieval supplied, so this — not the caller's `limit` — bounds the gain. |
| `SEARCH_RERANK_TIMEOUT_SECONDS` | `30.0` | Per-request budget. On expiry: retrieval order, `reranked: false`. |
| `SEARCH_RERANK_MAX_CONCURRENCY` | `1` | Rerank calls this process keeps in flight. Raise if your reranker has headroom. |

Full descriptions in [configuration.md](configuration.md).

## Which model?

`BAAI/bge-reranker-v2-m3` is the default and the only model this project ships a
**calibration curve** for (fitted on a 60-query labelled set, ADR-034). Any other
cross-encoder reranks correctly but reports
`relevance_source: uncalibrated` — the ordering is still right, the number is
just not a probability. Fit a curve for another model with
`scripts/fit_relevance_curves.py`.
