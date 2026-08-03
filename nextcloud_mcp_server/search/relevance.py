"""A [0, 1] relevance value on every search result (ADR-034).

Search returns three mutually incomparable numbers depending on how it ran — a
cosine similarity, a Qdrant RRF fused score bounded by ``2/k`` (~0.033 at the
default k=60), or a cross-encoder score. None of them can be shown to a user:
rendering a fused 0.033 as "3%" is what this module exists to stop, because at
k=60 that is 99% of the maximum achievable score, i.e. a near-perfect hit.

So every result carries ``relevance``, always in [0, 1], mapped from whichever
signal actually produced the ordering, plus a ``relevance_source`` saying which
mapping was used and — crucially — whether the number is a calibrated
probability or only an ordinal.

**The mappings are constants in this file, not configuration.** Cut-points an
operator has to tune are how you get a number whose meaning differs per
deployment, which defeats the point of showing one at all.

## Why two tiers, and why only one of them claims to be a probability

Measured on a 60-query labelled set (1200 (query, document) pairs, note 464925),
fitting each curve on the document-granularity pool and testing it on the
*chunk* pool — a genuine transfer test across a different retrieval shape and a
different prevalence (0.178 -> 0.274), not cross-validation:

| signal | in-sample ECE | transfer ECE | Brier vs always-base-rate |
|---|---|---|---|
| cross-encoder | 0.0414 | 0.0687 | 0.1662 vs 0.1990 (beats it) |
| RRF fusion | 0.0299 | 0.1142 | 0.1940 vs 0.1990 (barely) |

The cross-encoder curve degrades under the shift but survives: it still beats
the base rate, and the property that makes a filter useful — the spread of the
top-scoring row across queries — transfers intact (sd 0.305 -> 0.319).

The fusion curve does NOT survive. Its ECE quadruples, its Brier is level with
predicting the corpus base rate for everything, and its "0-10%" bucket actually
contained 19.3% relevant documents on the transfer set. That is expected rather
than surprising: an RRF score is an artifact of *rank*, so its relationship to
relevance is a property of the population it was fitted on, while a
cross-encoder score is computed from the query and the document themselves.

Hence: the cross-encoder tier reports a **calibrated probability**; the fusion
tier reports an **ordinal** in [0, 1] that orders results honestly and is not a
probability. Both are [0, 1] and filterable; only one may be rendered as a
percentage.

## What this does not do

* **It is not corpus-independent.** Every curve was fitted at prevalence 0.178
  on an OHR-Bench-derived corpus. On a corpus where relevant documents are much
  rarer, a displayed 0.70 overstates; where they are commoner, it understates.
  Ordering is unaffected — the mapping is monotone — so this shifts the number,
  never the ranking. ``fit_base_rate`` is published on every curve so a caller
  can reason about the direction. Correcting it without per-deployment labels is
  possible (SLD/EM prior correction) and is deliberately left to a follow-up.
* **It cannot abstain.** No per-result number answers "does this corpus contain
  an answer at all": on 15/15 deliberately unanswerable probes the top hit
  scored at or above the weakest answerable query's top hit. A high
  ``relevance`` means "best of what was retrieved", not "the answer is here".
"""

import math
from dataclasses import dataclass

# Source labels. Public: they appear in API responses and MCP tool output, so a
# client can decide whether it may render the value as a percentage.
RELEVANCE_CALIBRATED = "cross_encoder_calibrated"
RELEVANCE_ORDINAL = "fusion_ordinal"
RELEVANCE_UNCALIBRATED = "uncalibrated"


@dataclass(frozen=True)
class _Curve:
    """A fitted logistic (Platt) map from a raw signal to [0, 1].

    Platt rather than isotonic deliberately: the effective sample is 60
    QUERIES, not 1200 pairs — documents from one query share a retrieval
    context and a gold set — and isotonic only matches Platt above ~1000
    *independent* points (Niculescu-Mizil & Caruana, ICML 2005). A 100-step
    isotonic staircase fitted on 60 effective samples memorises the fit corpus.

    ``mu``/``sd`` standardise the raw score first, which is what lets one form
    cover signals whose natural scales differ by two orders of magnitude (an
    RRF artifact ~0.03 against a cross-encoder score ~0.5).
    """

    a: float
    b: float
    mu: float
    sd: float
    source: str
    fit_base_rate: float
    fit_n: int

    def __call__(self, score: float) -> float:
        z = (score - self.mu) / self.sd
        # Clamp before exp: a raw score far outside the fitted range (a
        # different reranker, a pathological BM25 value) would otherwise
        # overflow rather than saturating at the 0/1 the caller expects.
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, self.a * z + self.b))))


# Fitted 2026-08-03 on the 60-query enumeration set through the shipped search
# path (note 464925). Regenerate with scripts/fit_relevance_curves.py if the reranker
# or the fusion constant changes — a curve is only valid for the signal it was
# fitted on, which is why each is keyed by that signal's identity.
_CROSS_ENCODER_CURVES: dict[str, _Curve] = {
    # Keyed by model id with the gateway's provider prefix stripped, so
    # `local/BAAI/bge-reranker-v2-m3` and a bare `BAAI/bge-reranker-v2-m3`
    # resolve to the same curve — the prefix is a routing detail of the
    # gateway, not a property of the model.
    "BAAI/bge-reranker-v2-m3": _Curve(
        a=0.974596,
        b=-1.688786,
        mu=0.05437712,
        sd=0.11669424,
        source=RELEVANCE_CALIBRATED,
        fit_base_rate=0.178,
        fit_n=1200,
    ),
}

# The fusion tier. Ordinal, NOT calibrated — see the module docstring for the
# transfer measurement that decided this. Fitted on RRF at the default k=60;
# DBSF and dense-only cosine are different scales and get no curve, falling
# through to the uncalibrated squash below.
_FUSION_RRF_CURVE = _Curve(
    a=0.862457,
    b=-1.747789,
    mu=0.01543210,
    sd=0.00605003,
    source=RELEVANCE_ORDINAL,
    fit_base_rate=0.178,
    fit_n=1200,
)


def _normalize_model(model: str | None) -> str:
    """Strip the gateway's ``<provider>/`` routing prefix from a model id."""
    if not model:
        return ""
    # Only the FIRST segment is a provider prefix; model ids themselves contain
    # slashes (`BAAI/bge-reranker-v2-m3`), so split once and keep the remainder
    # when the head looks like a provider rather than an org.
    head, _, tail = model.partition("/")
    if tail and head in {"local", "openrouter", "mistral", "bedrock", "vllm"}:
        return tail
    return model


def relevance_for(
    *,
    rerank_score: float | None,
    score: float,
    fusion: str,
    algorithm: str,
    rerank_model: str | None,
) -> tuple[float, str]:
    """Map a result's raw signals onto ``(relevance, relevance_source)``.

    Always returns a value in [0, 1] — every result carries one, so a client
    never has to branch on whether a number exists. The *source* is what tells
    it how much the number may be read into.

    Preference order is by how much the signal knows about this query:

    1. a cross-encoder score with a fitted curve -> calibrated probability
    2. an RRF fused score -> ordinal (honest ordering, not a probability)
    3. anything else (DBSF, dense cosine, an unmapped reranker) -> uncalibrated

    ``rerank_score`` wins whenever present because it is what the results were
    ordered by; falling back to ``score`` there would report a number that
    disagrees with the ranking the caller is looking at.
    """
    if rerank_score is not None:
        curve = _CROSS_ENCODER_CURVES.get(_normalize_model(rerank_model))
        if curve is not None:
            return curve(rerank_score), curve.source
        # A reranker we have not fitted. Its score is still the ordering
        # signal, and cross-encoders conventionally emit [0, 1] — but some emit
        # logits, so clamp rather than trust. Reported as uncalibrated so no
        # client renders it as a probability.
        return min(1.0, max(0.0, rerank_score)), RELEVANCE_UNCALIBRATED

    if algorithm != "semantic" and fusion == "rrf":
        return _FUSION_RRF_CURVE(score), _FUSION_RRF_CURVE.source

    # Dense-only cosine is already [0, 1] and monotone in similarity; DBSF is
    # unbounded and only clamped. Neither has a fitted curve, so neither claims
    # to be one.
    return min(1.0, max(0.0, score)), RELEVANCE_UNCALIBRATED


def relevance_fit_base_rate(source: str) -> float | None:
    """The prevalence a source's curve was fitted at, or None if it has no fit.

    Exposed so a response can carry the caveat with the number instead of
    burying it in documentation nobody reads at the point of use.
    """
    if source == RELEVANCE_CALIBRATED:
        # Every fitted cross-encoder curve must agree on the prevalence, because
        # callers see ONE response-level figure while a single response can mix
        # sources: rerank_results appends rows it could not score (empty
        # excerpt, or an index the provider omitted) with rerank_score=None, so
        # those report the fusion source while the scored rows report this one.
        # Pinned by test_relevance.py rather than left as an assumption — a
        # future re-fit of one signal alone would otherwise silently make the
        # published figure wrong for half the rows.
        rates = {c.fit_base_rate for c in _CROSS_ENCODER_CURVES.values()}
        if len(rates) != 1:
            raise ValueError(
                "cross-encoder curves disagree on fit_base_rate "
                f"({sorted(rates)}); the response publishes a single figure, so "
                "re-fit them together or make the field per-result"
            )
        return rates.pop()
    if source == RELEVANCE_ORDINAL:
        return _FUSION_RRF_CURVE.fit_base_rate
    return None
