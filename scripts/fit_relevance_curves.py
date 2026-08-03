"""Fit the calibration curves that ship as constants, and validate them honestly.

Two signals get a curve:
  cross-encoder  BAAI/bge-reranker-v2-m3  (the gateway model)
  fusion         Qdrant RRF fused score   (k=60, the default)

Validation design is deliberately harder than cross-validation. Fitting on the
DOCUMENT-granularity pool and testing on the CHUNK pool is a genuine transfer
test: different retrieval shape, different base rate (0.178 vs 0.274), disjoint
candidate sets. In-sample CV would only show the fit is self-consistent; this
shows whether the number survives a population it was not fitted on -- which is
the actual claim a shipped curve makes.

Platt (2 parameters on a standardised score) rather than isotonic: the effective
sample is 60 QUERIES, not 1200 pairs, and isotonic only matches Platt above
~1000 independent points (Niculescu-Mizil & Caruana, ICML 2005). A 100-step
isotonic staircase fitted on 60 effective samples is memorising.
"""

import json
import math
import os
import statistics
import sys
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))


def key_of(title):
    t = (title or "").strip()
    for ext in (".pdf", ".json", ".PDF"):
        if t.endswith(ext):
            t = t[: -len(ext)]
    return os.path.basename(t)


def load(path):
    """Read a JSONL artifact.

    The path is resolved and checked before opening. This script only runs
    offline from a developer shell, so the risk is theoretical — but it does
    take its inputs from argv, and a validated path costs two lines.
    """
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"not a regular file: {resolved}")
    with resolved.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _labelled_rows(rows, gold, field, qid):
    """One query's distinct-document (score, label, qid) triples.

    Split out of pool() to keep each function's branching legible — and under
    Sonar's cognitive-complexity limit. The dedup-by-document-key logic is
    unchanged.
    """
    out, seen = [], set()
    for row in rows:
        k = key_of(row.get("title", ""))
        if not k or k in seen:
            continue
        seen.add(k)
        v = row.get(field)
        if v is not None:
            out.append((float(v), 1 if k in gold else 0, qid))
    return out


def pool(plan_rows, result_rows, field):
    """[(score, label, qid)] over distinct documents for the given score field."""
    plan_by_id = {p["ID"]: p for p in plan_rows}
    out = []
    for r in result_rows:
        p = plan_by_id.get(r["id"])
        if not p or not p.get("gold_docs"):
            continue
        out.extend(
            _labelled_rows(r.get("results", []), set(p["gold_docs"]), field, r["id"])
        )
    return out


def fit_platt(train, iters=4000, lr=0.5):
    """Returns (a, b, mu, sd). Standardising first keeps one learning rate valid
    for an RRF artifact (~0.03) and a cross-encoder score (~0.5) alike."""
    mu = statistics.fmean(s for s, _, _ in train)
    sd = statistics.pstdev([s for s, _, _ in train]) or 1.0
    a, b, n = 1.0, 0.0, len(train)
    for _ in range(iters):
        ga = gb = 0.0
        for s, y, _ in train:
            z = (s - mu) / sd
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, a * z + b))))
            ga += (p - y) * z
            gb += p - y
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b, mu, sd


def predict(params, x):
    a, b, mu, sd = params
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, a * (x - mu) / sd + b))))


def brier(p, y):
    return sum((a - b) ** 2 for a, b in zip(p, y)) / len(y)


def ece(p, y, bins=10):
    t = 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        idx = [
            j for j, v in enumerate(p) if lo <= v < hi or (i == bins - 1 and v >= 1.0)
        ]
        if idx:
            conf = sum(p[j] for j in idx) / len(idx)
            acc = sum(y[j] for j in idx) / len(idx)
            t += len(idx) / len(p) * abs(conf - acc)
    return t


def reliability(p, y):
    rows = []
    for lo, hi in [
        (0, 0.1),
        (0.1, 0.3),
        (0.3, 0.5),
        (0.5, 0.7),
        (0.7, 0.9),
        (0.9, 1.01),
    ]:
        idx = [j for j, v in enumerate(p) if lo <= v < hi]
        if idx:
            rows.append(
                (f"{lo:.0%}-{hi:.0%}", len(idx), sum(y[j] for j in idx) / len(idx))
            )
    return rows


def top1_spread(params, triples):
    by_q = {}
    for s, _, q in triples:
        by_q.setdefault(q, []).append(predict(params, s))
    top1 = [max(v) for v in by_q.values()]
    return statistics.pstdev(top1), min(top1), max(top1)


def evaluate(name, params, fit_pool, test_pool, fit_base):
    print(f"\n{'=' * 78}\n{name}\n{'=' * 78}")
    a, b, mu, sd = params
    print(f"  params: a={a:.6f} b={b:.6f} mu={mu:.8f} sd={sd:.8f}")
    for label, data in (
        ("IN-SAMPLE  (document)", fit_pool),
        ("TRANSFER   (chunk)", test_pool),
    ):
        if not data:
            continue
        p = [predict(params, s) for s, _, _ in data]
        y = [lab for _, lab, _ in data]
        base = sum(y) / len(y)
        sd_t, lo, hi = top1_spread(params, data)
        print(f"\n  {label}   n={len(data)}  base rate={base:.3f}")
        print(
            f"    Brier {brier(p, y):.4f}   ECE {ece(p, y):.4f}   "
            f"(always-base-rate Brier {brier([base] * len(y), y):.4f})"
        )
        print(f"    top-1 spread sd={sd_t:.3f}  range {lo:.2f}-{hi:.2f}")
        print(f"    {'shown':>10} {'n':>5} {'actual':>8}")
        for band, n, actual in reliability(p, y):
            print(f"    {band:>10} {n:>5} {actual:>8.3f}")
    print(
        f"\n  NOTE: fitted at base rate {fit_base:.3f}. A deployment corpus with a "
        f"different\n  prevalence shifts the number (ordering is unaffected — the map is monotone)."
    )


def main():
    plan = load(sys.argv[1])
    doc = load(sys.argv[2])
    chunk = load(sys.argv[3]) if len(sys.argv) > 3 else []

    out = {}
    for name, field in (
        ("cross_encoder_bge_reranker_v2_m3", "rerank_score"),
        ("fusion_rrf", "score"),
    ):
        fit_pool = pool(plan, doc, field)
        test_pool = pool(plan, chunk, field) if chunk else []
        if not fit_pool:
            continue
        base = sum(y for _, y, _ in fit_pool) / len(fit_pool)
        params = fit_platt(fit_pool)
        evaluate(name, params, fit_pool, test_pool, base)
        a, b, mu, sd = params
        out[name] = {
            "a": a,
            "b": b,
            "mu": mu,
            "sd": sd,
            "fit_base_rate": base,
            "fit_n": len(fit_pool),
        }

    dest = os.path.join(BASE, "curves.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\n\nwrote {dest}")
    print(json.dumps(out, indent=2))


main()
