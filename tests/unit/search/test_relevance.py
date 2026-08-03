"""The [0, 1] relevance value every search result carries (ADR-034).

What matters here is not the exact numbers — those come from a fit that will be
refreshed — but the properties a caller is entitled to rely on: the value always
exists, always lies in [0, 1], is monotone in the underlying signal, and never
claims to be a probability when the mapping behind it is not one.
"""

import pytest

from nextcloud_mcp_server.search.relevance import (
    RELEVANCE_CALIBRATED,
    RELEVANCE_ORDINAL,
    RELEVANCE_UNCALIBRATED,
    relevance_fit_base_rate,
    relevance_for,
)

pytestmark = pytest.mark.unit

_M3 = "BAAI/bge-reranker-v2-m3"


def _rel(**kw):
    args = {
        "rerank_score": None,
        "score": 0.02,
        "fusion": "rrf",
        "algorithm": "hybrid",
        "rerank_model": _M3,
    }
    args.update(kw)
    return relevance_for(**args)


# --- the bug this exists to prevent -------------------------------------------


def test_a_near_perfect_rrf_hit_is_not_reported_as_three_percent():
    """The reproducing case from Deck #958.

    Qdrant RRF at k=60 is bounded by 2/k = 0.0333, so the top hit in that report
    scored 0.03306 — 99.2% of the maximum achievable score, a near-perfect
    match — and the UI rendered it as "3%". Any mapping that leaves a caller
    able to make that mistake has failed at its one job.
    """
    value, source = _rel(score=0.03306)

    assert value > 0.5, (
        f"a 99.2%-of-ceiling RRF hit mapped to {value:.3f}; the whole point is "
        "that the raw 0.033 must not reach a user as a low number"
    )
    assert source == RELEVANCE_ORDINAL


def test_the_rrf_scale_is_spread_across_the_range_not_crushed_at_zero():
    """Raw RRF spans [0, 0.0333]; naive rendering crushes everything into the
    bottom 3% of a percentage bar. The mapping must actually use the range."""
    low, _ = _rel(score=0.005)
    mid, _ = _rel(score=0.017)
    high, _ = _rel(score=0.033)

    assert high - low > 0.4, "the mapped range is still crushed"
    assert low < mid < high


# --- properties a caller relies on --------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rerank_score": 0.9},
        {"rerank_score": 0.0},
        {"rerank_score": -8.0},  # a reranker emitting logits
        {"rerank_score": 250.0},
        {"score": 0.0},
        {"score": 1e9},  # unbounded raw BM25
        {"score": 0.5, "fusion": "dbsf"},
        {"score": 0.5, "algorithm": "semantic"},
        {"rerank_score": 0.5, "rerank_model": "some/unfitted-model"},
        {"rerank_score": 0.5, "rerank_model": None},
    ],
)
def test_always_within_the_unit_interval(kwargs):
    """Every result carries a value in [0, 1] — no NaN, no overflow, no None —
    so a client never has to branch on whether the number exists."""
    value, source = _rel(**kwargs)

    assert 0.0 <= value <= 1.0
    assert source in {
        RELEVANCE_CALIBRATED,
        RELEVANCE_ORDINAL,
        RELEVANCE_UNCALIBRATED,
    }


def test_monotone_in_the_cross_encoder_score():
    """Ranking and relevance must never disagree: a caller sorting by either one
    gets the same order."""
    values = [_rel(rerank_score=s)[0] for s in (0.0, 0.05, 0.2, 0.5, 0.9)]

    assert values == sorted(values)


def test_monotone_in_the_fused_score():
    values = [_rel(score=s)[0] for s in (0.0, 0.008, 0.017, 0.025, 0.033)]

    assert values == sorted(values)


# --- source selection ---------------------------------------------------------


def test_a_fitted_cross_encoder_reports_calibrated():
    _, source = _rel(rerank_score=0.42)

    assert source == RELEVANCE_CALIBRATED


def test_the_gateway_provider_prefix_resolves_to_the_same_curve():
    """The gateway routes on a `<provider>/` prefix, so the same model arrives as
    `local/BAAI/...` there and bare `BAAI/...` elsewhere. Treating those as
    different models would silently drop a fitted curve."""
    bare, bare_src = _rel(rerank_score=0.42, rerank_model=_M3)
    prefixed, prefixed_src = _rel(rerank_score=0.42, rerank_model=f"local/{_M3}")

    assert bare == prefixed
    assert bare_src == prefixed_src == RELEVANCE_CALIBRATED


def test_an_unfitted_reranker_is_reported_uncalibrated_not_guessed():
    """Better to say "no fitted mapping" than to apply another model's curve —
    that is how a number acquires a meaning it cannot back."""
    value, source = _rel(rerank_score=0.72, rerank_model="vendor/unknown-reranker")

    assert source == RELEVANCE_UNCALIBRATED
    assert value == pytest.approx(0.72)


def test_rerank_score_takes_precedence_over_the_fused_score():
    """Results are ORDERED by the rerank score when present, so relevance must
    come from the same signal or the number contradicts the ranking."""
    with_rerank, source = _rel(rerank_score=0.9, score=0.001)
    fused_only, _ = _rel(score=0.001)

    assert source == RELEVANCE_CALIBRATED
    assert with_rerank > fused_only


def test_dbsf_gets_no_fitted_curve():
    """The fit is RRF-specific; DBSF sums normalised per-retriever scores on a
    different, unbounded scale."""
    _, source = _rel(score=0.8, fusion="dbsf")

    assert source == RELEVANCE_UNCALIBRATED


def test_dense_only_search_gets_no_fitted_curve():
    """Dense-only returns a cosine similarity, not a fused score."""
    _, source = _rel(score=0.8, algorithm="semantic")

    assert source == RELEVANCE_UNCALIBRATED


# --- the honesty guarantees ---------------------------------------------------


def test_only_the_cross_encoder_source_claims_to_be_a_probability():
    """The transfer measurement is the reason: fitting on the document pool and
    testing on the chunk pool (prevalence 0.178 -> 0.274) left the cross-encoder
    curve beating the base rate, while the fusion curve's ECE quadrupled to
    0.1142 and its "0-10%" bucket actually held 19.3% relevant documents. The
    fusion tier orders honestly; it must not be rendered as a percentage."""
    assert relevance_fit_base_rate(RELEVANCE_CALIBRATED) == pytest.approx(0.178)
    assert relevance_fit_base_rate(RELEVANCE_ORDINAL) == pytest.approx(0.178)
    assert relevance_fit_base_rate(RELEVANCE_UNCALIBRATED) is None


def test_the_fit_base_rate_is_published_with_the_number():
    """A curve fitted at prevalence 0.178 overstates on a rarer corpus and
    understates on a denser one. Callers can only reason about the direction if
    the fit prevalence ships alongside the value."""
    assert relevance_fit_base_rate(RELEVANCE_CALIBRATED) is not None
