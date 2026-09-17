# retrieval_eval — OHR-Bench retrieval / generation eval harness

Self-contained, reproducible benchmark for the document-processing retrieval
pipeline. Reconstructs the lost `chunk_eval2.py` scratchpad (notes 390421 /
390460) as committed code. Where `scripts/chunk_density_sweep.py` measures only
**density** (chunks/MB, offline), this measures **quality** end to end:

```
extract (pypdfium2_fast) → chunk (PageAwareChunker) → embed (gateway/Ollama)
  → index (networked Qdrant: dense + BM25 sparse) → hybrid retrieve (RRF/DBSF)
  → optional rerank → score (OHR-Bench official LCS / page-hit / F1)
```

Used to benchmark three "new-model" levers across all 7 OHR-Bench domains:
**embedding model**, **reranker**, **generation model** (Deck board 12 card
#651; productionization design in note 390473).

## Prerequisites
- `~/Downloads/OHR-Bench/<domain>/*.pdf` (1,261 born-digital PDFs) and
  `~/Software/OHR-Bench/data/qas_v2.json` (gold Q&A).
- A **networked** Qdrant (`QDRANT_URL` + `QDRANT_API_KEY`) — directory mode
  throttles indexing throughput (note 390460); it is intentionally unsupported.
- Tailnet access to the embedding gateway (`EMBED_GATEWAY_URL`, default
  `astrolabe-gateway-dev.tail148d5.ts.net`, no bearer) and, for the self-hosted
  embedder, Ollama (`OLLAMA_HOST`). These are exported by the repo `.envrc`.

## Metric fidelity
`metrics.py` copies OHR-Bench `src/metric/common.py` (`lcs_score`, `f1_score`,
`exact_match_score`, `normalize_answer`) verbatim, so numbers are
leaderboard-comparable. Page-gating mirrors `src/tasks/retrieval.py` with one
documented adaptation: packed chunks span a page RANGE, so a chunk counts for
every page it covers (the rule used for packed configs in notes 390421/390460).
Unit-tested in `tests/unit/test_retrieval_eval_metrics.py`.

## Usage
```bash
OUT=./retrieval_eval_out   # gitignored

# 1. Sample a stratified plan (40 questions/domain, all 7 domains)
uv run python -m scripts.retrieval_eval plan --per-domain 40 --out $OUT/plan.jsonl

# 2. Index one (embedder × strategy) cell (one Qdrant collection per cell)
uv run python -m scripts.retrieval_eval index \
    --embedder mistral-embed --strategy page-pack@4096 --recreate --out-dir $OUT

# 3. Retrieval eval (± rerank), scored + sliced by domain
uv run python -m scripts.retrieval_eval eval \
    --embedder mistral-embed --strategy page-pack@4096 --plan $OUT/plan.jsonl \
    --rerank strong --out-dir $OUT      # rerank: none | strong | cheap

# 4. Generation eval (F1/EM) with an OpenRouter chat model
uv run python -m scripts.retrieval_eval generate \
    --embedder mistral-embed --strategy page-pack@4096 --plan $OUT/plan.jsonl \
    --chat-model openrouter/openai/gpt-4o-mini --out-dir $OUT
```

Registries (`__main__.py`): `EMBEDDERS` (mistral-embed, titan-v2, te3-small,
te3-large, arctic-110m), `STRATEGIES` (page-pack@4096 default + prod control),
`RERANKERS` (none / strong=`openrouter/cohere/rerank-v3.5` /
cheap=`Xenova/ms-marco-MiniLM-L-6-v2`), `CHAT_MODELS`. Smoke any cell fast with
`--domains finance --limit 2`.
```
```
