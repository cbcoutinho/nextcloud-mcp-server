"""Fidelity tests for the OHR-Bench metric port in scripts.retrieval_eval.

These pin the ported ``lcs_score`` / ``f1_score`` / page-gating to hand-worked
values so the committed harness provably matches OHR-Bench's official metrics
(the reason note 390434 flagged re-scoring on LCS in the first place).
"""

import math

import pytest

from scripts.retrieval_eval import metrics

pytestmark = pytest.mark.unit


def test_normalize_answer_strips_punct_articles_case():
    assert metrics.normalize_answer("The, QUICK  fox!") == "quick fox"


def test_lcs_score_full_partial_empty():
    # gold fully covered by prediction -> recall 1.0
    assert metrics.lcs_score("the quick brown fox", "quick fox") == pytest.approx(1.0)
    # only half the gold tokens present -> 0.5
    assert metrics.lcs_score("quick", "quick fox") == pytest.approx(0.5)
    # empty gold -> 0.5 sentinel (OHR-Bench convention)
    assert metrics.lcs_score("anything", "") == pytest.approx(0.5)


def test_lcs_is_subsequence_not_substring():
    # tokens must appear in order but not contiguously
    assert metrics.lcs_score("a x b y c", "a b c") == pytest.approx(1.0)


def test_f1_score_identical_partial_none_and_yesno_guard():
    assert metrics.f1_score("total 842", "total 842") == pytest.approx(1.0)
    assert metrics.f1_score("elephant", "842") == pytest.approx(0.0)
    # pred=[cat,sat] gt=[cat]: p=1/2, r=1/1 -> F1=0.6667
    assert metrics.f1_score("the cat sat", "cat") == pytest.approx(2 / 3)
    # yes/no/noanswer guard
    assert metrics.f1_score("yes", "no") == 0.0


def test_exact_match_normalizes():
    assert metrics.exact_match_score("The 842.", "842") == 1
    assert metrics.exact_match_score("843", "842") == 0


def test_covered_pages_range_and_offset():
    assert metrics.covered_pages(4, 6, 0) == {4, 5, 6}
    assert metrics.covered_pages(4, 6, 1) == {3, 4, 5}
    assert metrics.covered_pages(7, None, 0) == {7}
    assert metrics.covered_pages(None, None, 0) == set()


def _plan(**over):
    base = {
        "ID": "q1",
        "doc_name": "finance/X",
        "doc_type": "finance",
        "evidence_page_no": 5,
        "evidence_source": "table",
        "evidence_context": "total 842",
    }
    base.update(over)
    return base


def test_score_retrieval_packed_chunk_hits_page_range():
    plan = _plan()
    results = [
        {
            "doc_key": "finance/X",
            "page_number": 4,
            "page_end": 6,
            "text": "row total 842 row",
        },
        {"doc_key": "other/Y", "page_number": 5, "page_end": 5, "text": "noise"},
    ]
    s = metrics.score_retrieval(plan, results, offset=0)
    assert s["doc_hit"] == 1.0
    assert s["page_hit"] == 1.0
    assert s["page_lcs"] == pytest.approx(
        1.0
    )  # gold span fully present on the covered page


def test_score_retrieval_wrong_doc_is_miss():
    plan = _plan()
    results = [
        {"doc_key": "finance/Z", "page_number": 5, "page_end": 5, "text": "total 842"}
    ]
    s = metrics.score_retrieval(plan, results, offset=0)
    assert s["doc_hit"] == 0.0
    assert s["page_hit"] == 0.0
    assert s["page_lcs"] == 0.0


def test_score_retrieval_doc_hit_without_page_hit():
    plan = _plan(evidence_page_no=99)
    results = [
        {"doc_key": "finance/X", "page_number": 5, "page_end": 5, "text": "total 842"}
    ]
    s = metrics.score_retrieval(plan, results, offset=0)
    assert s["doc_hit"] == 1.0
    assert s["page_hit"] == 0.0


def test_calibrate_offset_picks_best():
    # gold page 5, retrieved page_number 6 -> only offset=1 makes it a hit
    plans = {"q1": _plan(evidence_page_no=5)}
    results = {
        "q1": [
            {
                "doc_key": "finance/X",
                "page_number": 6,
                "page_end": 6,
                "text": "total 842",
            }
        ]
    }
    assert metrics.calibrate_offset(plans, results) == 1


def test_aggregate_means_and_empty():
    plans = {"q1": _plan(), "q2": _plan(ID="q2")}
    results = {
        "q1": [
            {
                "doc_key": "finance/X",
                "page_number": 5,
                "page_end": 5,
                "text": "total 842",
            }
        ],
        "q2": [{"doc_key": "nope/W", "page_number": 1, "page_end": 1, "text": "x"}],
    }
    m = metrics.aggregate(plans, results, ["q1", "q2"], offset=0)
    assert m is not None
    assert m["n"] == 2.0
    assert m["doc_hit"] == pytest.approx(0.5)
    assert metrics.aggregate(plans, results, [], offset=0) is None


def test_score_generation_best_over_answers():
    s = metrics.score_generation("842", ["841", "842"])
    assert s["f1"] == pytest.approx(1.0)
    assert s["em"] == 1.0
    assert not math.isnan(s["f1"])


# ---------------------------------------------------------------------------
# Rank-sensitive metrics
#
# The page-gated family cannot see ordering: page_hit/doc_hit are set
# membership and page_lcs joins every matching chunk before scoring. These
# tests pin the family that CAN, because a reranker's entire job is invisible
# to the other one.
# ---------------------------------------------------------------------------
def _hit(page=5, text="total 842", doc="finance/X"):
    return {"doc_key": doc, "page_number": page, "page_end": page, "text": text}


def _miss(doc="other/Y", page=1):
    return {"doc_key": doc, "page_number": page, "page_end": page, "text": "x"}


def test_gold_rank_is_one_based_and_finds_first_gold_page():
    plan = _plan()
    assert metrics.gold_rank(plan, [_hit()], offset=0) == 1
    assert metrics.gold_rank(plan, [_miss(), _hit()], offset=0) == 2
    assert metrics.gold_rank(plan, [_miss(), _miss("z/Z"), _hit()], offset=0) == 3


def test_gold_rank_is_none_when_gold_never_appears():
    assert metrics.gold_rank(_plan(), [_miss(), _miss("z/Z")], offset=0) is None


def test_rank_metrics_see_reordering_that_page_lcs_cannot():
    """The whole reason this family exists.

    Same two result SETS, different ORDER. page_lcs/page_hit are identical
    because they only ask whether the gold chunk is present; Success@1 and MRR
    separate them. Measured on OHR-Bench this gap was 14x: +0.011 page_lcs
    versus +0.140 Success@1 on the same runs.
    """
    plans = {"q1": _plan()}
    gold_first = {"q1": [_hit(), _miss()]}
    gold_second = {"q1": [_miss(), _hit()]}

    a = metrics.aggregate(plans, gold_first, ["q1"], offset=0)
    b = metrics.aggregate(plans, gold_second, ["q1"], offset=0)
    assert a is not None and b is not None

    # Blind to the reorder...
    assert a["page_lcs"] == pytest.approx(b["page_lcs"])
    assert a["page_hit"] == pytest.approx(b["page_hit"])
    # ...sensitive to it.
    assert a["success_at_1"] == 1.0
    assert b["success_at_1"] == 0.0
    assert a["mrr"] == pytest.approx(1.0)
    assert b["mrr"] == pytest.approx(0.5)


def test_success_denominator_is_all_queries_not_just_found_ones():
    """A query whose gold never surfaced is a failure at rank 1, not an absent
    sample. Dividing by the found count would let a system that retrieves FEWER
    golds report a better Success@1."""
    plans = {"q1": _plan(), "q2": _plan(ID="q2")}
    results = {"q1": [_hit()], "q2": [_miss()]}

    m = metrics.aggregate(plans, results, ["q1", "q2"], offset=0)
    assert m is not None
    assert m["found"] == 1.0
    assert m["success_at_1"] == pytest.approx(0.5)  # not 1.0
    assert m["mrr"] == pytest.approx(0.5)


def test_mean_gold_rank_averages_found_only_and_is_zero_when_none_found():
    plans = {"q1": _plan(), "q2": _plan(ID="q2")}
    results = {"q1": [_miss(), _miss("z/Z"), _hit()], "q2": [_miss()]}

    m = metrics.aggregate(plans, results, ["q1", "q2"], offset=0)
    assert m is not None
    assert m["mean_gold_rank"] == pytest.approx(3.0)  # q2 excluded, not scored 0

    none_found = metrics.aggregate(plans, {"q1": [_miss()]}, ["q1"], offset=0)
    assert none_found is not None
    assert none_found["mean_gold_rank"] == 0.0
    assert none_found["found"] == 0.0


def test_success_at_3_is_inclusive_of_rank_three():
    plans = {"q1": _plan()}
    at3 = {"q1": [_miss(), _miss("z/Z"), _hit()]}
    at4 = {"q1": [_miss(), _miss("z/Z"), _miss("w/W"), _hit()]}

    m3 = metrics.aggregate(plans, at3, ["q1"], offset=0)
    m4 = metrics.aggregate(plans, at4, ["q1"], offset=0)
    assert m3 is not None and m4 is not None
    assert m3["success_at_3"] == 1.0
    assert m4["success_at_3"] == 0.0
