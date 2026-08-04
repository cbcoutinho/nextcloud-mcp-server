"""`min_relevance` — the filter that acts on the number the caller was shown.

Its whole reason to exist is that `score_threshold` cannot do this job:
`score_threshold` is pushed into Qdrant and applied to the raw retrieval score
BEFORE dedup, reranking and verify-on-read, so it is a recall cut that can drop
the very row reranking would have promoted. This filter runs last, on the mapped
[0, 1] value, so "at least this relevant" means what it says.
"""

import pytest

from nextcloud_mcp_server.search.algorithms import SearchResult
from nextcloud_mcp_server.search.relevance import filter_by_relevance, relevance_for

pytestmark = pytest.mark.unit

_M3 = "BAAI/bge-reranker-v2-m3"


def _row(score=0.02, rerank_score=None, id="1"):
    return SearchResult(
        id=id,
        doc_type="file",
        title=f"d{id}",
        excerpt="t",
        score=score,
        rerank_score=rerank_score,
    )


def _filter(rows, min_relevance, **kw):
    args = {"fusion": "rrf", "algorithm": "hybrid", "rerank_model": _M3}
    args.update(kw)
    return filter_by_relevance(rows, min_relevance=min_relevance, **args)


def test_zero_is_a_no_op_and_returns_the_same_list():
    """The default must cost nothing — not even mapping every row."""
    rows = [_row(score=s, id=str(i)) for i, s in enumerate((0.033, 0.016, 0.004))]

    assert _filter(rows, 0.0) is rows


def test_drops_only_rows_below_the_cut():
    rows = [_row(score=s, id=str(i)) for i, s in enumerate((0.033, 0.016, 0.004))]

    kept = _filter(rows, 0.3)

    # 0.033 -> ~0.68, 0.016 -> ~0.16, 0.004 -> ~0.03 (see test_relevance.py)
    assert [r.id for r in kept] == ["0"]


def test_the_cut_matches_the_value_reported_on_the_result():
    """A user filtering at 0.5 must keep exactly the rows displaying >= 0.5 —
    if the filter and the displayed number disagreed, the control would be
    unusable no matter how well calibrated either one was."""
    rows = [
        _row(score=s, id=str(i)) for i, s in enumerate((0.033, 0.025, 0.016, 0.004))
    ]

    kept = _filter(rows, 0.5)

    for r in rows:
        value, _ = relevance_for(
            rerank_score=r.rerank_score,
            score=r.score,
            fusion="rrf",
            algorithm="hybrid",
            rerank_model=_M3,
        )
        assert (r in kept) == (value >= 0.5)


def test_preserves_order():
    """Filtering is a cut, not a re-rank."""
    rows = [_row(score=s, id=str(i)) for i, s in enumerate((0.033, 0.030, 0.028))]

    assert [r.id for r in _filter(rows, 0.2)] == ["0", "1", "2"]


def test_filters_on_the_rerank_score_when_present():
    """Results are ordered by the cross-encoder score when reranking ran, so the
    filter has to act on the same signal or it would cut against a different
    ordering than the one the caller sees."""
    strong = _row(score=0.001, rerank_score=0.9, id="strong")
    weak = _row(score=0.033, rerank_score=0.001, id="weak")

    kept = _filter([strong, weak], 0.5)

    assert [r.id for r in kept] == ["strong"]


def test_applies_to_uncalibrated_sources_too():
    """Every source is monotone in whatever ordered the results, so the cut is
    meaningful even where the value is not a probability. Refusing to filter
    there would make the control silently inert on DBSF or dense-only search."""
    rows = [_row(score=s, id=str(i)) for i, s in enumerate((0.9, 0.2))]

    kept = _filter(rows, 0.5, fusion="dbsf")

    assert [r.id for r in kept] == ["0"]


def test_an_impossible_cut_returns_nothing_rather_than_a_best_effort():
    """Search cannot abstain, so this is the only way a caller gets an empty
    answer for a query the corpus has no answer to — it must not silently fall
    back to returning the top row anyway."""
    rows = [_row(score=s, id=str(i)) for i, s in enumerate((0.016, 0.004))]

    assert _filter(rows, 1.0) == []
