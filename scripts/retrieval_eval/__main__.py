"""CLI for the OHR-Bench retrieval/generation eval harness.

Env (see repo ``.envrc``):
  EMBED_GATEWAY_URL   default https://astrolabe-gateway-dev.tail148d5.ts.net
  OLLAMA_HOST         default https://ollama.internal.coutinho.io
  QDRANT_URL          default http://localhost:6333
  QDRANT_API_KEY      Qdrant auth (required by the networked container)

Typical run (one embedder × strategy cell):
  uv run python -m scripts.retrieval_eval plan --per-domain 40 --out $OUT/plan.jsonl
  uv run python -m scripts.retrieval_eval index --embedder mistral-embed --strategy page-pack@4096
  uv run python -m scripts.retrieval_eval eval  --embedder mistral-embed --strategy page-pack@4096 \
      --plan $OUT/plan.jsonl --rerank none --out-dir $OUT
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import time
from collections import defaultdict
from pathlib import Path

import anyio
import httpx
from qdrant_client import AsyncQdrantClient

from . import metrics
from .clients import (
    Bm25Encoder,
    EmbedderSpec,
    GatewayChat,
    GatewayReranker,
    LocalCrossEncoderReranker,
    make_embedder,
)
from .ocr import MistralSyncOCR, SuryaBatchOCR
from .pipeline import (
    IndexStats,
    clone_collection,
    embed_and_upsert,
    ensure_collection,
    eur_per_gib_mo,
    extract_and_chunk,
    index_corpus,
    iter_corpus,
    records_from_ocr,
    retrieve,
)

logger = logging.getLogger("retrieval_eval")

DEFAULT_CORPUS = Path("~/Downloads/OHR-Bench").expanduser()
DEFAULT_QAS = Path("~/Software/OHR-Bench/data/qas_v2.json").expanduser()
GATEWAY_URL = os.environ.get(
    "EMBED_GATEWAY_URL", "https://astrolabe-gateway-dev.tail148d5.ts.net"
)
OLLAMA_URL = os.environ.get("OLLAMA_HOST") or os.environ.get(
    "OLLAMA_BASE_URL", "https://ollama.internal.coutinho.io"
)

# --- model registry (discovered 2026-07-12; Deck #651) ---------------------
EMBEDDERS: dict[str, EmbedderSpec] = {
    "mistral-embed": EmbedderSpec(
        "mistral-embed", "gateway", "mistral/mistral-embed", 1024
    ),
    "titan-v2": EmbedderSpec(
        "titan-v2", "gateway", "bedrock/amazon.titan-embed-text-v2:0", 1024
    ),
    "te3-small": EmbedderSpec(
        "te3-small", "gateway", "openrouter/openai/text-embedding-3-small", 1536
    ),
    "te3-large": EmbedderSpec(
        "te3-large", "gateway", "openrouter/openai/text-embedding-3-large", 3072
    ),
    "arctic-110m": EmbedderSpec(
        "arctic-110m", "ollama", "snowflake-arctic-embed:110m", 768
    ),
    # Self-hosted open embedder. Never benchmarked before, and it is what a
    # gateway-free self-hoster running Infinity/vLLM actually uses.
    "bge-m3": EmbedderSpec("bge-m3", "gateway", "local/BAAI/bge-m3", 1024),
    "titan-v1": EmbedderSpec(
        "titan-v1", "gateway", "bedrock/amazon.titan-embed-text-v1", 1536
    ),
}

# strategy name -> (chunk_size, pack_pages, collection slug)
STRATEGIES: dict[str, tuple[int, bool, str]] = {
    "page-pack@4096": (4096, True, "pp4096"),
    "page-aware@2048": (2048, False, "pa2048"),  # production baseline / control
    "page-pack@2048": (2048, True, "pp2048"),
    "page-aware@4096": (4096, False, "pa4096"),
    # Bracket the tested 2048/4096 pair so the chunk-size axis has a shape, not
    # just two points. 512 is below a typical page, 8192 packs several.
    "page-pack@1024": (1024, True, "pp1024"),
    "page-pack@8192": (8192, True, "pp8192"),
    "page-aware@1024": (1024, False, "pa1024"),
}

# rerank presets -> (kind, model) ; kind in {"none","gateway","local"}
RERANKERS: dict[str, tuple[str, str]] = {
    "none": ("none", ""),
    # THE GAP this run exists to close: `local/BAAI/bge-reranker-v2-m3` is the
    # SHIPPED default (SEARCH_RERANK_MODEL) and the only model ADR-034 fitted a
    # calibration curve for, yet it has never been measured for RETRIEVAL
    # quality. Prior work benchmarked cohere-v3.5 (+) and ms-marco (-) — neither
    # is what we ship.
    "bge": ("gateway", "local/BAAI/bge-reranker-v2-m3"),
    # Strong reference point. Re-pointed: the gateway no longer serves
    # `openrouter/cohere/rerank-v3.5`; the Bedrock-routed id is the live one.
    "strong": ("gateway", "bedrock/cohere.rerank-v3-5:0"),
    "amazon": ("gateway", "bedrock/amazon.rerank-v1:0"),
    "cheap": ("local", "Xenova/ms-marco-MiniLM-L-6-v2"),
}

CHAT_MODELS = [
    "openrouter/openai/gpt-4o-mini",
    "openrouter/meta-llama/llama-3.3-70b-instruct",
    "openrouter/qwen/qwen-2.5-72b-instruct",
    "bedrock/eu.amazon.nova-lite-v1:0",
    # Native mistral/ provider (same vendor as the production embedder) — routed
    # by the gateway even though /v1/chat/models doesn't advertise them.
    "mistral/ministral-8b-latest",
    "mistral/ministral-3b-latest",
    "mistral/mistral-small-latest",
]


def collection_name(embedder: str, strategy: str, suffix: str = "") -> str:
    slug = STRATEGIES[strategy][2]
    return f"reval_{EMBEDDERS[embedder].slug}_{slug}{suffix}"


# OCR engine -> (mode, model). surya=batch (triggers leaf.cloud GPU); mistral=sync (cloud).
OCR_ENGINES: dict[str, tuple[str, str]] = {
    "surya": ("batch", "surya/surya-ocr-2"),
    "mistral": ("sync", "mistral/mistral-ocr-4-0"),
}


def _gateway_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=GATEWAY_URL, timeout=httpx.Timeout(180.0))


def _ollama_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=OLLAMA_URL, timeout=httpx.Timeout(180.0))


def _qdrant() -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        api_key=os.environ.get("QDRANT_API_KEY"),
        timeout=120,
    )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def cmd_plan(args: argparse.Namespace) -> None:
    qas = json.loads(Path(args.qas).read_text())
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for q in qas:
        by_domain[q["doc_type"]].append(q)
    domains = args.domains or sorted(by_domain)
    rng = random.Random(args.seed)
    sample: list[dict] = []
    for domain in domains:
        pool = by_domain.get(domain, [])
        sample += rng.sample(pool, min(args.per_domain, len(pool)))
    rng.shuffle(sample)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for q in sample:
            f.write(
                json.dumps(
                    {
                        "ID": q["ID"],
                        "doc_name": q["doc_name"],
                        "doc_type": q["doc_type"],
                        "question": q["questions"],
                        "answer": q["answers"],
                        "evidence_source": q["evidence_source"],
                        "evidence_page_no": q["evidence_page_no"],
                        "evidence_context": q["evidence_context"],
                        # The Nextcloud content type the gold document lives in
                        # (note / deck_card / file / ...). Carried on every plan
                        # row so the eval can slice by it — see the BY SOURCE
                        # TYPE block in _report_eval for why that slice is
                        # mandatory on a mixed corpus.
                        #
                        # OHR-Bench is PDFs only, so a generator that does not
                        # set it yields None and the slice is omitted rather
                        # than fabricating a single-bucket comparison.
                        "source_type": q.get("source_type"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    counts = defaultdict(int)
    for q in sample:
        counts[q["doc_type"]] += 1
    print(f"Wrote {len(sample)} questions to {out}")
    for d in sorted(counts):
        print(f"  {d:16} {counts[d]}")


def _load_plan(path: str) -> dict[str, dict]:
    plans: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            plans[p["ID"]] = p
    return plans


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------
async def _run_index(args: argparse.Namespace) -> None:
    spec = EMBEDDERS[args.embedder]
    chunk_size, pack_pages, _ = STRATEGIES[args.strategy]
    coll = collection_name(args.embedder, args.strategy, args.suffix)
    limiter = anyio.CapacityLimiter(args.embed_concurrency)
    bm25 = Bm25Encoder()

    async with _gateway_client() as gw, _ollama_client() as oll:
        qdrant = _qdrant()
        try:
            embedder = make_embedder(spec, gateway=gw, ollama=oll, limiter=limiter)
            # confirm live dimension before creating the collection
            probe, _ = await embedder.embed(["dimension probe"])
            dim = len(probe[0])
            if dim != spec.dim:
                logger.warning(
                    "declared dim %d != live dim %d for %s", spec.dim, dim, spec.name
                )
            spec.dim = dim
            created = await ensure_collection(qdrant, coll, dim, recreate=args.recreate)
            if not created:
                print(
                    f"collection {coll} exists — skipping (use --recreate to rebuild)"
                )
                return
            doc_allow: set[str] | None = None
            if args.from_plan:
                doc_allow = {p["doc_name"] for p in _load_plan(args.from_plan).values()}
                logger.info(
                    "indexing %d gold docs from %s", len(doc_allow), args.from_plan
                )
            t0 = time.monotonic()
            stats = await index_corpus(
                qdrant=qdrant,
                collection=coll,
                embedder=embedder,
                bm25=bm25,
                spec=spec,
                corpus=Path(args.corpus).expanduser(),
                domains=args.domains,
                limit=args.limit,
                chunk_size=chunk_size,
                pack_pages=pack_pages,
                doc_allow=doc_allow,
            )
        finally:
            await qdrant.close()

    elapsed = time.monotonic() - t0
    report = {
        "collection": coll,
        "embedder": spec.name,
        "model": spec.model,
        "dim": stats.dim,
        "strategy": args.strategy,
        "docs": stats.docs,
        "empty_docs": stats.empty_docs,  # scanned/no-text PDFs, not indexed
        "chunks": stats.total_chunks,
        "embed_tokens": stats.embed_tokens,
        "seconds": round(elapsed, 1),
        "by_domain": {
            d: {
                "chunks": dd.chunks,
                "chunks_per_mb": round(dd.chunks_per_mb, 1),
                "eur_per_gib_mo": round(eur_per_gib_mo(dd.chunks_per_mb, stats.dim), 3),
            }
            for d, dd in sorted(stats.by_domain.items())
        },
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"density_{coll}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


# ---------------------------------------------------------------------------
# eval (retrieve + optional rerank + score)
# ---------------------------------------------------------------------------
def _make_reranker(name: str, gw: httpx.AsyncClient, limiter: anyio.CapacityLimiter):
    kind, model = RERANKERS[name]
    if kind == "none":
        return None
    if kind == "gateway":
        return GatewayReranker(gw, model, limiter)
    return LocalCrossEncoderReranker(model)


async def _run_eval(args: argparse.Namespace) -> None:
    spec = EMBEDDERS[args.embedder]
    coll = collection_name(args.embedder, args.strategy, args.suffix)
    plans = _load_plan(args.plan)
    limiter = anyio.CapacityLimiter(args.embed_concurrency)
    bm25 = Bm25Encoder()
    results: dict[str, list[dict]] = {}
    q_limiter = anyio.CapacityLimiter(args.query_concurrency)
    lock = anyio.Lock()

    async with _gateway_client() as gw, _ollama_client() as oll:
        qdrant = _qdrant()
        embedder = make_embedder(spec, gateway=gw, ollama=oll, limiter=limiter)
        reranker = _make_reranker(args.rerank, gw, limiter)

        # A reranker reorders a broad pool into the top-k; hybrid-only fetches 2k.
        #
        # CONFOUND: with the defaults that is 50 vs 20, so a rerank-vs-none A/B
        # compares TWO things at once — reordering skill AND a 2.5x deeper
        # candidate pool. The pool alone lifts recall (more gold documents make
        # the top-k at all), which is not what "does reranking help" is asking.
        # `--fetch` forces both arms to the same depth so the only difference is
        # the reordering. Note 390487's "gain grows with pool size
        # (+0.026@20 -> +0.051@50)" is exactly the signature this produces.
        fetch = args.fetch or (args.rerank_pool if reranker is not None else args.k * 2)

        async def handle(pid: str, plan: dict) -> None:
            async with q_limiter:
                cands = await retrieve(
                    qdrant=qdrant,
                    collection=coll,
                    query=plan["question"],
                    embedder=embedder,
                    bm25=bm25,
                    k=args.k,
                    fusion=args.fusion,
                    fetch=fetch,
                    rescore=args.rescore,
                    oversampling=args.oversampling,
                )
                if reranker is not None and cands:
                    order = await reranker.rerank(
                        plan["question"], [c["text"] for c in cands], top_n=args.k
                    )
                    cands = [cands[i] for i, _ in order]
                else:
                    cands = cands[: args.k]
                async with lock:
                    results[pid] = cands

        try:
            async with anyio.create_task_group() as tg:
                for pid, plan in plans.items():
                    tg.start_soon(handle, pid, plan)
        finally:
            await qdrant.close()

    _report_retrieval(args, coll, plans, results)


def _report_retrieval(
    args: argparse.Namespace,
    coll: str,
    plans: dict[str, dict],
    results: dict[str, list[dict]],
) -> None:
    offset = metrics.calibrate_offset(plans, results)
    answered = [p for p in plans if p in results]
    domains = sorted({plans[p]["doc_type"] for p in answered})
    sources = sorted({plans[p]["evidence_source"] for p in answered})

    def fmt(label: str, m: dict | None) -> str:
        """One line per slice, BOTH metric families side by side.

        The rank-sensitive columns are not optional extras. On the same runs the
        two families have disagreed in magnitude by 14x and, across chunk sizes,
        in sign — so printing only the page-gated ones lets the reader draw a
        conclusion the data does not support.
        """
        if not m:
            return f"  {label:22} (no data)"
        return (
            f"  {label:22} n={int(m['n']):<4} page_lcs={m['page_lcs']:.3f} "
            f"page_hit={m['page_hit']:.3f} doc_hit={m['doc_hit']:.3f} "
            f"doc_lcs={m['doc_lcs']:.3f} | S@1={m['success_at_1']:.3f} "
            f"S@3={m['success_at_3']:.3f} MRR={m['mrr']:.3f} "
            f"rank={m['mean_gold_rank']:.2f} found={int(m['found'])}"
        )

    print(f"\n=== {coll}  rerank={args.rerank}  fusion={args.fusion}  k={args.k} ===")
    print(
        f"offset={offset} (gold = page_number - offset), answered {len(answered)}/{len(plans)}"
    )
    print("OVERALL")
    print(fmt("all", metrics.aggregate(plans, results, answered, offset)))
    print("BY DOMAIN")
    per_domain = {}
    for d in domains:
        m = metrics.aggregate(
            plans, results, [p for p in answered if plans[p]["doc_type"] == d], offset
        )
        per_domain[d] = m
        print(fmt(d, m))
    print("BY EVIDENCE SOURCE")
    for s in sources:
        print(
            fmt(
                s,
                metrics.aggregate(
                    plans,
                    results,
                    [p for p in answered if plans[p]["evidence_source"] == s],
                    offset,
                ),
            )
        )

    # BY SOURCE TYPE — the Nextcloud content type a gold document lives in
    # (note / deck_card / file / mail_message / ...), carried on the plan row as
    # `source_type`.
    #
    # This slice is MANDATORY for any mixed-content corpus and must never be
    # pooled away into a single headline number. The failure mode it exists to
    # expose is LENGTH ASYMMETRY: a 20-word Deck card and an 800-token PDF chunk
    # compete in the same Qdrant collection, and the density spread across our
    # own doc types is roughly 47x (1.2 -> 57 chunks/MB). A system that is
    # excellent on files and useless on cards posts a respectable aggregate
    # score, and the aggregate is what gets quoted.
    #
    # Absent on the OHR-Bench corpus, which is PDFs only — the slice simply does
    # not print there rather than inventing a single "file" bucket that would
    # imply a comparison the corpus cannot support.
    per_source_type = {}
    source_types = sorted({st for p in answered if (st := plans[p].get("source_type"))})
    if source_types:
        print("BY SOURCE TYPE")
        for st in source_types:
            m = metrics.aggregate(
                plans,
                results,
                [p for p in answered if plans[p].get("source_type") == st],
                offset,
            )
            per_source_type[st] = m
            print(fmt(st, m))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{coll}_rr-{args.rerank}_{args.fusion}"
    (out_dir / f"metrics_{tag}.json").write_text(
        json.dumps(
            {
                "collection": coll,
                "rerank": args.rerank,
                "fusion": args.fusion,
                "k": args.k,
                "offset": offset,
                "answered": len(answered),
                "overall": metrics.aggregate(plans, results, answered, offset),
                "by_domain": per_domain,
                # Empty on a single-type corpus (OHR-Bench). Present and
                # non-empty is the signal that per-type numbers exist and the
                # overall figure must not be quoted on its own.
                "by_source_type": per_source_type,
            },
            indent=2,
        )
    )
    (out_dir / f"results_{tag}.jsonl").write_text(
        "\n".join(json.dumps({"id": pid, "results": results[pid]}) for pid in answered)
    )


# ---------------------------------------------------------------------------
# generate (retrieve context -> chat -> F1/EM)
# ---------------------------------------------------------------------------
_GEN_PROMPT = (
    "Answer the question using ONLY the context. Reply with the shortest exact answer "
    "(a number, name, or short phrase); no explanation.\n\nContext:\n{context}\n\n"
    "Question: {question}\nAnswer:"
)


async def _run_generate(args: argparse.Namespace) -> None:
    spec = EMBEDDERS[args.embedder]
    coll = collection_name(args.embedder, args.strategy, args.suffix)
    plans = _load_plan(args.plan)
    limiter = anyio.CapacityLimiter(args.embed_concurrency)
    chat_limiter = anyio.CapacityLimiter(args.chat_concurrency)
    bm25 = Bm25Encoder()
    scored: dict[str, dict] = {}
    lock = anyio.Lock()

    async with _gateway_client() as gw, _ollama_client() as oll:
        qdrant = _qdrant()
        embedder = make_embedder(spec, gateway=gw, ollama=oll, limiter=limiter)
        reranker = _make_reranker(args.rerank, gw, limiter)
        chat = GatewayChat(gw, args.chat_model, chat_limiter)

        async def handle(pid: str, plan: dict) -> None:
            cands = await retrieve(
                qdrant=qdrant,
                collection=coll,
                query=plan["question"],
                embedder=embedder,
                bm25=bm25,
                k=args.k,
                fusion=args.fusion,
            )
            if reranker is not None and cands:
                order = await reranker.rerank(
                    plan["question"], [c["text"] for c in cands], top_n=args.k
                )
                cands = [cands[i] for i, _ in order]
            else:
                cands = cands[: args.k]
            context = "\n\n".join(c["text"] for c in cands)
            try:
                answer = await chat.generate(
                    _GEN_PROMPT.format(
                        context=context[: args.max_context_chars],
                        question=plan["question"],
                    ),
                    max_tokens=args.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("gen failed %s: %s", pid, exc)
                return
            s = metrics.score_generation(answer, plan["answer"])
            async with lock:
                scored[pid] = {**s, "answer": answer}

        try:
            async with anyio.create_task_group() as tg:
                for pid, plan in plans.items():
                    tg.start_soon(handle, pid, plan)
        finally:
            await qdrant.close()

    _report_generation(args, coll, plans, scored)


def _report_generation(
    args: argparse.Namespace, coll: str, plans: dict[str, dict], scored: dict[str, dict]
) -> None:
    answered = [p for p in plans if p in scored]
    by_domain: dict[str, list[str]] = defaultdict(list)
    for p in answered:
        by_domain[plans[p]["doc_type"]].append(p)

    def mean(pids: list[str], key: str) -> float:
        return sum(scored[p][key] for p in pids) / len(pids) if pids else 0.0

    print(
        f"\n=== GENERATION {coll}  chat={args.chat_model}  rerank={args.rerank}  k={args.k} ==="
    )
    print(f"answered {len(answered)}/{len(plans)}")
    print(
        f"  {'all':16} n={len(answered):<4} F1={mean(answered, 'f1'):.3f} EM={mean(answered, 'em'):.3f}"
    )
    for d in sorted(by_domain):
        pids = by_domain[d]
        print(
            f"  {d:16} n={len(pids):<4} F1={mean(pids, 'f1'):.3f} EM={mean(pids, 'em'):.3f}"
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chat_slug = args.chat_model.replace("/", "_")
    (out_dir / f"gen_{coll}_{chat_slug}_rr-{args.rerank}.json").write_text(
        json.dumps(
            {
                "collection": coll,
                "chat_model": args.chat_model,
                "rerank": args.rerank,
                "overall": {
                    "f1": mean(answered, "f1"),
                    "em": mean(answered, "em"),
                    "n": len(answered),
                },
                "by_domain": {
                    d: {"f1": mean(p, "f1"), "em": mean(p, "em"), "n": len(p)}
                    for d, p in sorted(by_domain.items())
                },
            },
            indent=2,
        )
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ocr — fill the born-digital blind spot: OCR the scanned gold docs and index
# ---------------------------------------------------------------------------
async def _find_pending(
    corpus: Path, gold: set[str], chunk_size: int, pack_pages: bool
) -> list[tuple[str, str, Path]]:
    """Gold docs that yield NO born-digital text (scanned) -> need OCR."""
    pending: list[tuple[str, str, Path]] = []
    for doc_key, domain, pdf in iter_corpus(corpus, None, None, gold):
        try:
            chunks = await extract_and_chunk(
                pdf, chunk_size=chunk_size, pack_pages=pack_pages
            )
        except Exception:  # noqa: BLE001 - bad PDF => treat as OCR-pending
            chunks = []
        if not chunks:
            pending.append((doc_key, domain, pdf))
    return pending


async def _run_ocr(args: argparse.Namespace) -> None:
    spec = EMBEDDERS[args.embedder]
    chunk_size, pack_pages, _ = STRATEGIES[args.strategy]
    coll = collection_name(args.embedder, args.strategy, args.suffix)
    mode, model = OCR_ENGINES[args.engine]
    gold = {p["doc_name"] for p in _load_plan(args.plan).values()}
    corpus = Path(args.corpus).expanduser()

    pending = await _find_pending(corpus, gold, chunk_size, pack_pages)
    if args.limit_docs:
        pending = pending[: args.limit_docs]
    logger.info(
        "%d OCR-pending gold docs; engine=%s model=%s mode=%s",
        len(pending),
        args.engine,
        model,
        mode,
    )
    dom_by = {dk: dom for dk, dom, _ in pending}
    size_by = {dk: pdf.stat().st_size for dk, _, pdf in pending}
    bm25 = Bm25Encoder()
    t0 = time.monotonic()

    # --- Phase A: OCR on its own gateway client ---
    results: dict[str, list[str]] = {}
    async with _gateway_client() as gw:
        if mode == "sync":
            ocr = MistralSyncOCR(gw, model, anyio.CapacityLimiter(args.ocr_concurrency))
            lock = anyio.Lock()

            async def do_one(doc_key: str, pdf: Path) -> None:
                try:
                    pages = await ocr.ocr_doc(pdf.read_bytes())
                except Exception as exc:  # noqa: BLE001 - one bad doc must not abort
                    logger.warning("ocr failed %s: %s", doc_key, exc)
                    return
                async with lock:
                    results[doc_key] = pages

            async with anyio.create_task_group() as tg:
                for doc_key, _, pdf in pending:
                    tg.start_soon(do_one, doc_key, pdf)
        else:  # batch (surya) — triggers leaf.cloud GPU (unless --reuse-job)
            batch = SuryaBatchOCR(gw, model, poll_seconds=args.poll_seconds)
            docs = (
                []
                if args.reuse_job
                else [(dk, pdf.read_bytes()) for dk, _, pdf in pending]
            )
            results = await batch.ocr_docs(docs, reuse_job_id=args.reuse_job)
    ocr_seconds = time.monotonic() - t0

    items = [
        (dk, dom_by[dk], size_by[dk], results[dk]) for dk in results if dk in dom_by
    ]
    records = await records_from_ocr(
        items, chunk_size=chunk_size, pack_pages=pack_pages
    )

    # --- Phase B: embed + upsert on a FRESH gateway client. The OCR poll can hold
    # the client idle for many minutes; reusing that aged pool at high embed
    # concurrency triggered a raw ssl.SSLError. A fresh client sidesteps it.
    limiter = anyio.CapacityLimiter(args.embed_concurrency)
    stats = IndexStats(dim=spec.dim)
    async with _gateway_client() as gw2, _ollama_client() as oll:
        qdrant = _qdrant()
        try:
            if not await qdrant.collection_exists(coll):
                raise SystemExit(
                    f"collection {coll} missing — index born-digital first: "
                    f"index --embedder {args.embedder} --suffix {args.suffix} "
                    f"--from-plan {args.plan} --recreate"
                )
            embedder = make_embedder(spec, gateway=gw2, ollama=oll, limiter=limiter)
            await embed_and_upsert(
                qdrant=qdrant,
                collection=coll,
                embedder=embedder,
                bm25=bm25,
                records=records,
                stats=stats,
            )
        finally:
            await qdrant.close()

    report = {
        "collection": coll,
        "engine": args.engine,
        "model": model,
        "pending_docs": len(pending),
        "ocr_docs_returned": len(results),
        "indexed_docs": stats.docs,
        "indexed_chunks": stats.total_chunks,
        "ocr_seconds": round(ocr_seconds, 1),
        "by_domain": {d: dd.chunks for d, dd in sorted(stats.by_domain.items())},
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"ocr_{coll}_{args.engine}.json").write_text(
        json.dumps(report, indent=2)
    )
    print(json.dumps(report, indent=2))


# ---------------------------------------------------------------------------
# quantize — clone a collection applying int8/binary quantization (no re-embed)
# ---------------------------------------------------------------------------
async def _run_quantize(args: argparse.Namespace) -> None:
    source = collection_name(args.embedder, args.strategy, args.from_suffix)
    target = collection_name(args.embedder, args.strategy, args.suffix)
    qdrant = _qdrant()
    try:
        if not await qdrant.collection_exists(source):
            raise SystemExit(f"source collection {source} missing")
        info = await qdrant.get_collection(source)
        dim = info.config.params.vectors["dense"].size  # type: ignore[index,union-attr]
        t0 = time.monotonic()
        copied = await clone_collection(
            qdrant, source, target, dim, quantization=args.mode
        )
    finally:
        await qdrant.close()
    print(
        json.dumps(
            {
                "source": source,
                "target": target,
                "mode": args.mode,
                "points_copied": copied,
                "seconds": round(time.monotonic() - t0, 1),
            },
            indent=2,
        )
    )


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--embedder", required=True, choices=sorted(EMBEDDERS))
    p.add_argument("--strategy", default="page-pack@4096", choices=sorted(STRATEGIES))
    p.add_argument(
        "--suffix",
        default="",
        help="collection-name suffix (e.g. _surya, _mocr) to keep OCR-engine "
        "variants separate from the born-digital baseline",
    )
    p.add_argument("--out-dir", default="retrieval_eval_out")
    # The gateway serves each request's texts serially (~1.6 s/text) but
    # parallelizes across requests, scaling near-linearly to ~48 (measured, 0
    # errors). High concurrency is what makes the full sweep tractable.
    p.add_argument("--embed-concurrency", type=int, default=48)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    # Silence per-request HTTP chatter (httpx/qdrant/fastembed) so progress logs
    # and the final report are readable; keep our own logger at INFO.
    for noisy in ("httpx", "httpcore", "qdrant_client", "fastembed", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="stratified-sample questions -> plan.jsonl")
    p.add_argument("--qas", default=str(DEFAULT_QAS))
    p.add_argument("--per-domain", type=int, default=40)
    p.add_argument("--domains", nargs="*", default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="retrieval_eval_out/plan.jsonl")
    p.set_defaults(func=lambda a: cmd_plan(a))

    p = sub.add_parser("index", help="index the corpus into Qdrant for one cell")
    _add_common(p)
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    p.add_argument("--domains", nargs="*", default=None)
    p.add_argument(
        "--limit", type=int, default=None, help="max PDFs per domain (smoke)"
    )
    p.add_argument(
        "--from-plan",
        default=None,
        help="index only the gold docs referenced by this plan.jsonl "
        "(the gold-doc corpus of notes 390421/390460)",
    )
    p.add_argument("--recreate", action="store_true", help="drop + rebuild if exists")
    p.set_defaults(func=lambda a: anyio.run(_run_index, a))

    p = sub.add_parser("eval", help="retrieve (+rerank) + score against a plan")
    _add_common(p)
    p.add_argument("--plan", required=True)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--rerank", default="none", choices=sorted(RERANKERS))
    p.add_argument(
        "--rerank-pool",
        type=int,
        default=50,
        help="candidates fetched for the reranker to reorder into top-k (rerank only)",
    )
    p.add_argument(
        "--fetch",
        type=int,
        default=None,
        help=(
            "force the candidate depth for BOTH arms (matched-pool A/B). "
            "Without it the rerank arm fetches --rerank-pool while the "
            "baseline fetches 2k, which confounds reordering with pool depth."
        ),
    )
    p.add_argument("--fusion", default="rrf", choices=["rrf", "dbsf"])
    p.add_argument("--query-concurrency", type=int, default=12)
    p.add_argument(
        "--rescore",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="quantization rescore (recall recovery on int8/binary collections)",
    )
    p.add_argument(
        "--oversampling",
        type=float,
        default=None,
        help="quantization oversampling factor (widens the quantized shortlist; binary)",
    )
    p.set_defaults(func=lambda a: anyio.run(_run_eval, a))

    p = sub.add_parser(
        "quantize",
        help="clone a collection applying int8/binary quantization (no re-embed)",
    )
    _add_common(p)
    p.add_argument("--mode", required=True, choices=["none", "int8", "binary"])
    p.add_argument(
        "--from-suffix", default="", help="source collection suffix (default: baseline)"
    )
    p.set_defaults(func=lambda a: anyio.run(_run_quantize, a))

    p = sub.add_parser("generate", help="retrieve context -> chat -> F1/EM")
    _add_common(p)
    p.add_argument("--plan", required=True)
    p.add_argument("--chat-model", required=True)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--rerank", default="none", choices=sorted(RERANKERS))
    p.add_argument("--fusion", default="rrf", choices=["rrf", "dbsf"])
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--max-context-chars", type=int, default=12000)
    p.add_argument("--chat-concurrency", type=int, default=6)
    p.set_defaults(func=lambda a: anyio.run(_run_generate, a))

    p = sub.add_parser(
        "ocr", help="OCR the scanned gold docs and index into an existing collection"
    )
    _add_common(p)
    p.add_argument("--plan", required=True)
    p.add_argument("--engine", required=True, choices=sorted(OCR_ENGINES))
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    p.add_argument(
        "--ocr-concurrency", type=int, default=6, help="sync-OCR concurrency (mistral)"
    )
    p.add_argument(
        "--poll-seconds", type=float, default=10.0, help="batch poll interval (surya)"
    )
    p.add_argument(
        "--reuse-job",
        default=None,
        help="surya: re-index an already-completed batch job_id (no re-submit, no GPU)",
    )
    p.add_argument(
        "--limit-docs", type=int, default=None, help="cap pending docs (smoke)"
    )
    p.set_defaults(func=lambda a: anyio.run(_run_ocr, a))

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
