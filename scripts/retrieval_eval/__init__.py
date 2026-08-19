"""Self-contained OHR-Bench retrieval/generation eval harness (committed).

Reconstructs and supersedes the lost ``chunk_eval2.py`` scratchpad that produced
Nextcloud notes 390421 / 390460. Where ``scripts/chunk_density_sweep.py`` covers
only the *density* half (chunks/MB, no network), this package covers the
*quality* half end to end:

    extract (pypdfium2_fast) -> chunk (PageAwareChunker) -> embed (gateway/Ollama)
    -> index (networked Qdrant, dense + BM25 sparse) -> hybrid retrieve (RRF/DBSF)
    -> optional rerank -> score (OHR-Bench official LCS / page-hit / F1)

It is used to benchmark three "new-model" levers that prior notes left open:
embedding model, reranker, and generation model — across all 7 OHR-Bench domains.
See Deck board 12 card #651 and note 390473 (productionization design).

Run ``uv run python -m scripts.retrieval_eval --help`` for the CLI.

The metric functions in :mod:`scripts.retrieval_eval.metrics` are ported verbatim
from OHR-Bench (``src/metric/common.py``) so results are leaderboard-comparable.
"""
