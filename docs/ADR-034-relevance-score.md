# ADR-034: A [0, 1] relevance value on every search result

**Status:** Accepted
**Date:** 2026-08-03
**Supersedes:** nothing. **Related:** ADR-012 (multi-algorithm search), ADR-019
(verify-on-read), Deck #958.

## Context

Search returns a `score`, and callers reasonably assume it means something. It
does not — or rather, it means three different things depending on how the query
ran, and none of them is presentable:

| search type | what `score` is | range |
|---|---|---|
| `semantic` (dense only) | cosine similarity | [0, 1], not comparable across queries |
| `bm25` / `hybrid` with `fusion=rrf` | Qdrant RRF fused score | **[0, 2/k] = [0, 0.0333]** at the default k=60 |
| `bm25` / `hybrid` with `fusion=dbsf` | sum of normalised per-retriever scores | unbounded |
| any of the above with `rerank=true` | cross-encoder score | model-specific; some emit [0,1], some emit logits |

The RRF row is the one that caused real damage. Qdrant's RRF is `Σ 1/(k + rank)`,
so at k=60 the entire achievable range is [0, 0.0333]. A top hit scoring 0.03306
is at **99.2% of the maximum possible score** — a near-perfect match — and the
Astrolabe UI rendered it to users as **"3%"**. RRF scores are also quantised by
*which legs matched*: results matching one retrieval leg only score exactly
`1/60 = 0.01667`, so a naive `score / (2/k)` rescale pins every single-leg match
at exactly 0.5 forever.

Astrolabe 0.39.5 removed the percentage rather than keep lying. That left users
with a relevance slider filtering on a number they could not see, which is worse
than either extreme.

An earlier attempt added `strong`/`moderate`/`weak` bands with **operator-tunable
cut-points in config**. Rejected: that makes the meaning of the signal a property
of each deployment, which is exactly what stops a number being worth showing.

## Decision

**Every search result carries `relevance`, always in [0, 1], plus a
`relevance_source` saying which mapping produced it and whether that mapping is a
probability.** The mappings are constants in `nextcloud_mcp_server/search/relevance.py`,
never configuration.

| `relevance_source` | signal | may be rendered as a percentage? |
|---|---|---|
| `cross_encoder_calibrated` | a cross-encoder score with a fitted curve | **yes** — it is a calibrated P(relevant) |
| `fusion_ordinal` | Qdrant RRF at k=60 | **no** — monotone and filterable, but not a probability |
| `uncalibrated` | DBSF, dense-only cosine, or a reranker we ship no fit for | **no** |

Clients gate presentation on the source rather than guessing from magnitude.
`score` is left untouched, so `score_threshold` — applied inside Qdrant, before
reranking — keeps referring to exactly the quantity it always did.

### Why two tiers, and why only one is a probability

Fitted with Platt (2 parameters over a standardised score) on a 60-query
labelled set, 1200 (query, document) pairs, base rate 0.178 (note 464925).
Validation is a **transfer test, not cross-validation**: fit on the
document-granularity pool, test on the *chunk* pool — a different retrieval
shape and a different prevalence (0.178 → 0.274).

| signal | in-sample ECE | transfer ECE | Brier vs always-base-rate | top-1 spread |
|---|---|---|---|---|
| cross-encoder `bge-reranker-v2-m3` | 0.0414 | **0.0687** | 0.1662 vs 0.1990 ✓ | 0.305 → 0.319 |
| RRF fusion | 0.0299 | **0.1142** | 0.1940 vs 0.1990 ✗ | 0.160 → 0.170 |

The cross-encoder curve degrades under the shift but survives — it still beats
predicting the base rate, and the property that makes a filter useful (how much
the top row's value varies between queries) transfers intact.

The fusion curve does not survive: its ECE quadruples, its Brier is level with
predicting the corpus base rate for every document, and its "0-10%" bucket
actually contained **19.3%** relevant documents on the transfer set. This is
expected rather than surprising — an RRF score is an artifact of *rank*, so its
relationship to relevance is a property of the population it was fitted on, while
a cross-encoder score is computed from the query and the document themselves.

Platt rather than isotonic: the effective sample is **60 queries**, not 1200
pairs (documents from one query share a retrieval context and a gold set), and
isotonic only matches Platt above ~1000 *independent* points (Niculescu-Mizil &
Caruana, ICML 2005).

### Why not the alternatives

- **Percent-of-top-result.** Maps the top hit to 1.0 on *every* query, including
  ones that found nothing. As a filter, "≥80%" then means something different per
  query. Weaviate ships this as `relativeScoreFusion` and it is fine for fusion —
  it is not a relevance measure.
- **An ordinal band with configured cut-points.** Pushes calibration onto every
  operator and still shows the user no number.
- **A fixed vendor scale (Azure's 0–4).** Coherent, and the closest prior art,
  but it is ordinal by construction; we have the labelled data to do better on
  the tier that supports it.

## Consequences

- Clients get one field with one meaning per source, stable across deployments,
  with nothing to tune.
- A curve is only valid for the signal it was fitted on. Changing
  `SEARCH_RERANK_MODEL` to a model we ship no fit for degrades to
  `uncalibrated` — results still rank correctly and still carry a number, but the
  probability claim is dropped rather than faked.
- Re-fitting is a code change with a measurement behind it, reviewable as such.

## What this does NOT solve

Both are real and are deliberately out of scope here.

1. **Base-rate shift.** Every curve was fitted at prevalence 0.178. On a corpus
   where relevant documents are rarer, a displayed 0.70 overstates; where they
   are commoner, it understates. **Ordering is unaffected** — the mapping is
   monotone, so this shifts the number and never the ranking. The fit prevalence
   is published (`relevance_fit_base_rate`) so callers can reason about the
   direction. The fix that needs no per-deployment labels is unsupervised prior
   correction (SLD/EM), which works precisely in this regime — binary classes over
   a calibrated base (Esuli, Molinari & Sebastiani, *A Critical Reassessment of
   the SLD Algorithm*, TOIS 2021). Tracked separately.
2. **Abstention.** No per-result number answers "does this corpus contain an
   answer at all". On 15/15 deliberately unanswerable probes the top hit scored at
   or above the weakest answerable query's top hit. A high `relevance` means
   "best of what was retrieved", never "the answer is here". This is structural:
   it holds for every construction reviewed, calibrated or not.

## References

- Note 464925 — the measurements, including the transfer test and the rerank
  backend comparison.
- Deck #958 — the reproducing case and the rejected band design.
- Fitting script: `scripts/fit_relevance_curves.py` (regenerate the constants with it).
