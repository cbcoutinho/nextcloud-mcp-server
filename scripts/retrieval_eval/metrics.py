"""OHR-Bench metric port + retrieval/generation scoring.

The core string metrics (:func:`normalize_answer`, :func:`lcs_score`,
:func:`f1_score`, :func:`exact_match_score`) are copied faithfully from OHR-Bench
``src/metric/common.py`` so our numbers are directly comparable to the published
leaderboard. The retrieval page-gating mirrors ``src/tasks/retrieval.py`` (a hit =
a retrieved chunk from the gold doc whose page matches the evidence page), with
one documented adaptation: our packed chunks span a page RANGE
(``page_number..page_end``), so a chunk counts for every page it covers — this is
the same range-coverage rule used for the packed configs in note 390421/390460.

Pure module (no I/O, no network) so it is unit-tested in
``tests/unit/test_retrieval_eval_metrics.py``.
"""

from __future__ import annotations

import re
import string
from collections import Counter, defaultdict
from collections.abc import Iterable

# jieba is only needed to tokenize Chinese for F1; OHR-Bench uses it. It is not a
# repo dependency, so fall back to character tokens when a CJK string appears and
# jieba is unavailable (English — the bulk of the corpus — never needs it).
try:  # pragma: no cover - exercised only when the optional dep is installed
    import jieba  # type: ignore

    _HAVE_JIEBA = True
except ImportError:  # pragma: no cover
    jieba = None  # type: ignore
    _HAVE_JIEBA = False

_ARTICLES = re.compile(r"\b(a|an|the)\b")
_PUNC = set(string.punctuation)


# ---------------------------------------------------------------------------
# String metrics — verbatim from OHR-Bench src/metric/common.py
# ---------------------------------------------------------------------------
def normalize_answer(s: str) -> str:
    """Lowercase, strip punctuation + articles, collapse whitespace."""
    s = s.lower()
    s = "".join(ch for ch in s if ch not in _PUNC)
    s = _ARTICLES.sub(" ", s)
    return " ".join(s.split())


def lcs_score(prediction: str, ground_truth: str) -> float:
    """Word-level LCS recall of the gold span. ``A`` = gold, ``B`` = prediction.

    Returns ``len(LCS)/len(gold_tokens)``; ``0.5`` when the gold span is empty
    (matches OHR-Bench). This is OHR-Bench's official *retrieval* metric.
    """
    a = normalize_answer(ground_truth).split()
    b = normalize_answer(prediction).split()
    if len(a) == 0:
        return 0.5
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[len(a)][len(b)] / len(a)


def _has_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


def _tokens_for_f1(text: str) -> list[str]:
    normalized = normalize_answer(text)
    if _has_cjk(text):
        if _HAVE_JIEBA:
            return jieba.lcut(normalized)  # type: ignore[no-any-return]
        return list(normalized.replace(" ", ""))
    return normalized.split()


def f1_score(prediction: str, ground_truth: str) -> float:
    """Token-level F1 (OHR-Bench official *generation* metric)."""
    norm_pred = normalize_answer(prediction)
    norm_gt = normalize_answer(ground_truth)

    # yes/no/noanswer guard, verbatim from OHR-Bench.
    for special in ("yes", "no", "noanswer"):
        if norm_pred == special and norm_pred != norm_gt:
            return 0.0
        if norm_gt == special and norm_pred != norm_gt:
            return 0.0

    pred_tokens = _tokens_for_f1(prediction)
    gt_tokens = _tokens_for_f1(ground_truth)
    if not pred_tokens or not gt_tokens:
        return 0.0
    common = Counter(pred_tokens) & Counter(gt_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gt_tokens)
    return (2 * precision * recall) / (precision + recall)


def exact_match_score(prediction: str, ground_truth: str) -> int:
    """1 if normalized prediction equals normalized gold, else 0."""
    return 1 if normalize_answer(prediction) == normalize_answer(ground_truth) else 0


# ---------------------------------------------------------------------------
# Retrieval scoring (mirrors src/tasks/retrieval.py, range-aware for packing)
# ---------------------------------------------------------------------------
def gold_pages(evidence_page_no: int | list[int]) -> set[int]:
    if isinstance(evidence_page_no, list):
        return {int(p) for p in evidence_page_no}
    return {int(evidence_page_no)}


def covered_pages(
    page_number: int | None, page_end: int | None, offset: int
) -> set[int]:
    """Gold-page indices a (possibly packed) chunk covers, at a given offset.

    ``gold_page == astrolabe_page_number - offset`` (0- vs 1-index calibration).
    A packed chunk spans ``page_number..page_end`` inclusive.
    """
    if page_number is None:
        return set()
    end = page_end if page_end is not None else page_number
    lo, hi = min(page_number, end), max(page_number, end)
    return {p - offset for p in range(lo, hi + 1)}


def _as_text(context: str | list[str]) -> str:
    return "\n".join(context) if isinstance(context, list) else context


def score_retrieval(plan: dict, results: list[dict], offset: int) -> dict[str, float]:
    """Score one question's retrieval results at a given page offset.

    ``results`` items are ``{"doc_key", "page_number", "page_end", "text"}``.
    Returns ``page_lcs / page_hit / doc_hit / doc_lcs`` (OHR-Bench metrics).
    """
    gkey = plan["doc_name"]
    gpages = gold_pages(plan["evidence_page_no"])
    gctx = _as_text(plan["evidence_context"])

    doc_chunks = [r for r in results if r.get("doc_key") == gkey]
    page_chunks = [
        r
        for r in doc_chunks
        if covered_pages(r.get("page_number"), r.get("page_end"), offset) & gpages
    ]
    page_text = "\n\n".join(r.get("text", "") for r in page_chunks)
    doc_text = "\n\n".join(r.get("text", "") for r in doc_chunks)

    return {
        "page_lcs": lcs_score(page_text, gctx) if page_chunks else 0.0,
        "page_hit": 1.0 if page_chunks else 0.0,
        "doc_hit": 1.0 if doc_chunks else 0.0,
        "doc_lcs": lcs_score(doc_text, gctx) if doc_chunks else 0.0,
    }


def calibrate_offset(plans: dict[str, dict], results: dict[str, list[dict]]) -> int:
    """Pick the page offset (0 or 1) that yields the most page-hits.

    OHR-Bench gold pages may be 0- or 1-indexed relative to our page numbers;
    calibrate over the answered set exactly as the external harness does.
    """
    answered = [pid for pid in plans if pid in results]
    best_off, best_hits = 0, -1
    for off in (0, 1):
        hits = sum(
            score_retrieval(plans[pid], results[pid], off)["page_hit"]
            for pid in answered
        )
        if hits > best_hits:
            best_off, best_hits = off, hits
    return best_off


def gold_rank(plan: dict, results: list[dict], offset: int) -> int | None:
    """1-based rank of the first result on a gold page of the gold document.

    ``score_retrieval``'s metrics cannot see this. ``page_hit``/``doc_hit`` are
    pure set membership, and ``page_lcs`` JOINS every page-matching chunk before
    computing LCS — so a reranker that lifts the gold document from rank 8 to
    rank 1 moves none of them. Measured on OHR-Bench: enabling reranking changed
    the ``page_lcs`` of 12 of 280 queries (+0.011, p=0.034) while moving
    Success@1 by +0.140. The page-gated metric is structurally blind to
    ordering, which is most of what retrieval tuning actually changes.

    Returns ``None`` when the gold page never appears in the result list.
    """
    gkey = plan["doc_name"]
    gpages = gold_pages(plan["evidence_page_no"])
    for i, r in enumerate(results, start=1):
        if r.get("doc_key") != gkey:
            continue
        if covered_pages(r.get("page_number"), r.get("page_end"), offset) & gpages:
            return i
    return None


def aggregate(
    plans: dict[str, dict],
    results: dict[str, list[dict]],
    pids: Iterable[str],
    offset: int,
) -> dict[str, float] | None:
    """Mean of each retrieval metric over ``pids``; ``None`` if empty.

    Reports BOTH families, always, because they answer different questions and
    have been observed to disagree in sign on the same runs:

    * ``page_lcs`` / ``page_hit`` / ``doc_hit`` / ``doc_lcs`` — OHR-Bench's
      official page-gated metrics. Set-membership: did the gold evidence reach
      the top-k at all. Leaderboard-comparable, and blind to ordering.
    * ``success_at_1`` / ``success_at_3`` / ``mrr`` / ``mean_gold_rank`` —
      rank-sensitive. Did the right thing come FIRST.

    Emitting only the first family is how a 24.5% relative improvement in
    Success@1 gets reported as a marginal +0.011 and dismissed.
    """
    pids = [p for p in pids if p in results]
    n = len(pids)
    if n == 0:
        return None
    acc: dict[str, float] = defaultdict(float)
    ranks: list[int] = []
    for pid in pids:
        for k, v in score_retrieval(plans[pid], results[pid], offset).items():
            acc[k] += v
        r = gold_rank(plans[pid], results[pid], offset)
        if r is not None:
            ranks.append(r)
    return {
        "n": float(n),
        **{k: acc[k] / n for k in ("page_lcs", "page_hit", "doc_hit", "doc_lcs")},
        # Denominator is n, not len(ranks): a query whose gold never surfaced is
        # a failure at rank 1, not an absent sample. Dividing by len(ranks) would
        # let a system that retrieves fewer golds report a better Success@1.
        "success_at_1": sum(1 for r in ranks if r == 1) / n,
        "success_at_3": sum(1 for r in ranks if r <= 3) / n,
        "mrr": sum(1.0 / r for r in ranks) / n,
        # Mean over FOUND golds only — the average depth you must read to when
        # the answer is there at all. Undefined when nothing was found.
        "mean_gold_rank": (sum(ranks) / len(ranks)) if ranks else 0.0,
        "found": float(len(ranks)),
    }


# ---------------------------------------------------------------------------
# Generation scoring
# ---------------------------------------------------------------------------
def score_generation(prediction: str, answers: str | list[str]) -> dict[str, float]:
    """Best F1/EM of ``prediction`` over one or more gold answers."""
    golds = answers if isinstance(answers, list) else [answers]
    golds = [str(g) for g in golds] or [""]
    return {
        "f1": max(f1_score(prediction, g) for g in golds),
        "em": float(max(exact_match_score(prediction, g) for g in golds)),
    }
