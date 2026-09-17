"""Indexing + hybrid retrieval for the eval harness.

Mirrors the production hybrid stack so results transfer: extraction via
``pypdfium2_fast._extract`` (the born-digital tier-1 path), chunking via
``PageAwareChunker`` (the shipped default), a networked Qdrant collection with the
same named vectors (``dense`` + ``sparse``) and BM25 model (``Qdrant/bm25``), and
Qdrant-native RRF/DBSF fusion — the exact query shape of
``search/bm25_hybrid.py``. Directory-mode Qdrant is deliberately NOT supported
here: it throttles indexing throughput (note 390460).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from qdrant_client import AsyncQdrantClient, models

from nextcloud_mcp_server.document_processors.pypdfium2_fast import _extract
from nextcloud_mcp_server.vector.document_chunker import (
    DocumentChunker,
    PageAwareChunker,
)

from .clients import Bm25Encoder, EmbedderSpec, GatewayEmbedder, OllamaEmbedder
from .ocr import pages_to_extract

logger = logging.getLogger(__name__)

# €/GiB-mo dense-RAM carry: bytes/point = dim*4 (fp32) + ~2048 (sparse+payload+index
# overhead, calibrated so 1024-d == the note-390397 6,144 B/point), at €1.75/GB-mo.
_OVERHEAD_BYTES = 2048
_EUR_PER_GB_MO = 1.75


def eur_per_gib_mo(chunks_per_mb: float, dim: int) -> float:
    bytes_per_point = dim * 4 + _OVERHEAD_BYTES
    return chunks_per_mb * bytes_per_point * 1000 * _EUR_PER_GB_MO / 1e9


@dataclass
class Chunk:
    text: str
    page_number: int | None
    page_end: int | None


@dataclass
class DomainDensity:
    chunks: int = 0
    source_bytes: int = 0

    @property
    def chunks_per_mb(self) -> float:
        return (
            self.chunks / (self.source_bytes / 1_000_000) if self.source_bytes else 0.0
        )


@dataclass
class IndexStats:
    dim: int = 0
    docs: int = 0
    total_chunks: int = 0
    embed_tokens: int = 0
    # Docs that yielded no born-digital text (scanned / image-only PDFs) — NOT
    # indexed, so their questions auto-miss. Surfaced (not silently dropped) so a
    # coverage gap is visible: OHR-Bench includes scanned docs the tier-1
    # extractor can't read; that is the pipeline's real limitation, not a bug.
    empty_docs: int = 0
    by_domain: dict[str, DomainDensity] = field(
        default_factory=lambda: defaultdict(DomainDensity)
    )


def iter_corpus(
    corpus: Path,
    domains: list[str] | None,
    limit: int | None,
    doc_allow: set[str] | None = None,
) -> Iterator[tuple[str, str, Path]]:
    """Yield ``(doc_key, domain, pdf_path)`` for ``<domain>/<name>.pdf`` PDFs.

    ``doc_key`` is ``"<domain>/<name>"`` (no extension) — the exact form of
    OHR-Bench ``qas_v2.json`` ``doc_name``, so gold and indexed keys compare
    directly with no domain/extension juggling.

    ``doc_allow`` restricts to that set of ``doc_key``s — used by ``--from-plan``
    to index only the gold docs referenced by the sampled questions (the
    gold-doc corpus of notes 390421/390460), which is both faithful to prior
    methodology and far cheaper than the full 1,261-doc corpus.
    """
    dirs = domains or sorted(p.name for p in corpus.iterdir() if p.is_dir())
    for domain in dirs:
        domain_dir = corpus / domain
        if not domain_dir.is_dir():
            continue
        pdfs = sorted(domain_dir.rglob("*.pdf"))
        if limit is not None:
            pdfs = pdfs[:limit]
        for pdf in pdfs:
            doc_key = pdf.relative_to(corpus).with_suffix("").as_posix()
            if doc_allow is not None and doc_key not in doc_allow:
                continue
            yield doc_key, domain, pdf


async def extract_and_chunk(
    pdf: Path, *, chunk_size: int, pack_pages: bool
) -> list[Chunk]:
    """Extract born-digital text and chunk it with the page-aware chunker."""
    pdf_bytes = pdf.read_bytes()
    text, metadata = await anyio.to_thread.run_sync(_extract, pdf_bytes)  # type: ignore[attr-defined]
    if not text:
        return []
    boundaries = metadata.get("page_boundaries") or []
    if boundaries:
        chunker = PageAwareChunker(chunk_size=chunk_size, pack_pages=pack_pages)
        raw = await chunker.chunk_text(text, boundaries)
    else:
        raw = await DocumentChunker(chunk_size=chunk_size).chunk_text(text)
    return [Chunk(c.text, c.page_number, c.page_end) for c in raw if c.text.strip()]


async def records_from_ocr(
    items: list[tuple[str, str, int, list[str]]],
    *,
    chunk_size: int,
    pack_pages: bool,
) -> list[_DocRecord]:
    """Chunk OCR'd docs into ``_DocRecord``s via the same page-aware chunker.

    ``items`` = ``[(doc_key, domain, source_bytes, pages_markdown), ...]``. The OCR
    per-page markdown is reconstructed into the ``(text, page_boundaries)`` contract
    (`ocr.pages_to_extract`) so OCR text flows through the identical chunk path as
    born-digital text — same page-pack@N behavior, same page-range citation.
    """
    records: list[_DocRecord] = []
    for doc_key, domain, source_bytes, pages_md in items:
        if not any(p.strip() for p in pages_md):
            logger.warning("ocr produced no text: %s", doc_key)
            continue
        text, meta = pages_to_extract(pages_md)
        boundaries = meta["page_boundaries"]
        if boundaries:
            raw = await PageAwareChunker(
                chunk_size=chunk_size, pack_pages=pack_pages
            ).chunk_text(text, boundaries)
        else:
            raw = await DocumentChunker(chunk_size=chunk_size).chunk_text(text)
        chunks = [
            Chunk(c.text, c.page_number, c.page_end) for c in raw if c.text.strip()
        ]
        if chunks:
            records.append(_DocRecord(doc_key, domain, source_bytes, chunks))
    return records


async def ensure_collection(
    qdrant: AsyncQdrantClient,
    name: str,
    dim: int,
    *,
    recreate: bool,
    quantization: str = "none",
) -> bool:
    """Create the collection (dense + sparse). Returns True if (re)created.

    ``quantization`` in {none, int8, binary}: when set, the dense vector is
    quantized (int8 scalar or 1-bit binary) with the quantized form pinned in RAM
    (``always_ram``) and the original fp32 moved on-disk (``on_disk=True``) for
    rescoring. This is the RAM-footprint lever the cost model doesn't yet price.
    """
    exists = await qdrant.collection_exists(name)
    if exists and not recreate:
        return False
    if exists:
        await qdrant.delete_collection(name)
    on_disk = quantization != "none"
    await qdrant.create_collection(
        collection_name=name,
        vectors_config={
            "dense": models.VectorParams(
                size=dim, distance=models.Distance.COSINE, on_disk=on_disk
            )
        },
        sparse_vectors_config={"sparse": models.SparseVectorParams()},
        quantization_config=_quantization_config(quantization),
    )
    return True


def _quantization_config(mode: str):
    """Qdrant quantization_config for a mode; None for 'none'."""
    if mode == "int8":
        return models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8, always_ram=True
            )
        )
    if mode == "binary":
        return models.BinaryQuantization(
            binary=models.BinaryQuantizationConfig(always_ram=True)
        )
    if mode == "none":
        return None
    raise ValueError(f"unknown quantization mode: {mode!r}")


async def clone_collection(
    qdrant: AsyncQdrantClient,
    source: str,
    target: str,
    dim: int,
    *,
    quantization: str,
    batch: int = 512,
) -> int:
    """Copy source → target applying ``quantization`` — reuses vectors, no re-embed.

    Streams points (dense + sparse + payload) from an existing collection into a
    fresh one whose only difference is the quantization config. Lets us A/B RAM
    modes on identical embeddings without paying the gateway again.
    """
    await ensure_collection(
        qdrant, target, dim, recreate=True, quantization=quantization
    )
    copied = 0
    offset = None
    while True:
        points, offset = await qdrant.scroll(
            collection_name=source,
            limit=batch,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break
        await qdrant.upsert(
            collection_name=target,
            points=[
                models.PointStruct(
                    id=p.id,
                    vector=p.vector,  # type: ignore[arg-type]  # scroll w/ vectors -> dict
                    payload=p.payload,
                )
                for p in points
                if p.vector is not None
            ],
        )
        copied += len(points)
        if offset is None:
            break
    return copied


@dataclass
class _DocRecord:
    doc_key: str
    domain: str
    source_bytes: int
    chunks: list[Chunk]
    start: int = 0  # offset into the flat chunk arrays


async def index_corpus(
    *,
    qdrant: AsyncQdrantClient,
    collection: str,
    embedder: GatewayEmbedder | OllamaEmbedder,
    bm25: Bm25Encoder,
    spec: EmbedderSpec,
    corpus: Path,
    domains: list[str] | None,
    limit: int | None,
    chunk_size: int,
    pack_pages: bool,
    doc_allow: set[str] | None = None,
    extract_concurrency: int = 1,
    embed_batch: int = 8,
    sparse_batch: int = 512,
    upsert_concurrency: int = 8,
    upsert_batch: int = 256,
) -> IndexStats:
    """Extract → chunk → embed (dense+sparse) → upsert.

    Embedding is the bottleneck (the gateway serves each request's texts serially
    at ~1.6 s/text but parallelizes across requests — measured). So rather than
    embed each doc's chunks sequentially (which makes the slowest single doc
    dominate wall-clock), ALL chunks across ALL docs are flattened and embedded
    through one global concurrent pool bounded by the embedder's own
    ``CapacityLimiter``. Throughput then scales with that limiter (~29 chunks/s at
    48-way) regardless of per-doc size.
    """
    stats = IndexStats(dim=spec.dim)

    # --- Phase 1: extract + chunk all docs ---
    # pypdfium2 is NOT thread-safe: concurrent ``_extract`` calls corrupt the
    # native heap (``free(): invalid size`` -> SIGABRT). So extraction defaults to
    # serial (``extract_concurrency=1``). This costs nothing — extraction is fast
    # and the slow phase (embedding) is parallelized separately below.
    records: list[_DocRecord] = []
    ex_limiter = anyio.CapacityLimiter(extract_concurrency)
    ex_lock = anyio.Lock()

    async def do_extract(doc_key: str, domain: str, pdf: Path) -> None:
        async with ex_limiter:
            try:
                chunks = await extract_and_chunk(
                    pdf, chunk_size=chunk_size, pack_pages=pack_pages
                )
            except Exception as exc:  # noqa: BLE001 - a bad PDF must not abort the run
                logger.warning("skip %s: %s", doc_key, exc)
                return
            if not chunks:
                # No text layer (scanned/image PDF) — the tier-1 extractor's blind
                # spot. Record it so coverage is auditable rather than silent.
                async with ex_lock:
                    stats.empty_docs += 1
                logger.debug("no text layer (scanned?): %s", doc_key)
                return
            async with ex_lock:
                records.append(_DocRecord(doc_key, domain, pdf.stat().st_size, chunks))

    async with anyio.create_task_group() as tg:
        for doc_key, domain, pdf in iter_corpus(corpus, domains, limit, doc_allow):
            tg.start_soon(do_extract, doc_key, domain, pdf)

    logger.info(
        "extracted %d docs (%d had no text layer); embedding (dim=%d)...",
        len(records),
        stats.empty_docs,
        spec.dim,
    )
    await embed_and_upsert(
        qdrant=qdrant,
        collection=collection,
        embedder=embedder,
        bm25=bm25,
        records=records,
        stats=stats,
        embed_batch=embed_batch,
        sparse_batch=sparse_batch,
        upsert_concurrency=upsert_concurrency,
        upsert_batch=upsert_batch,
    )
    return stats


async def embed_and_upsert(
    *,
    qdrant: AsyncQdrantClient,
    collection: str,
    embedder: GatewayEmbedder | OllamaEmbedder,
    bm25: Bm25Encoder,
    records: list[_DocRecord],
    stats: IndexStats,
    embed_batch: int = 8,
    sparse_batch: int = 512,
    upsert_concurrency: int = 8,
    upsert_batch: int = 256,
) -> None:
    """Embed (dense+sparse) + upsert pre-chunked ``records`` into ``collection``.

    Shared by born-digital (`index_corpus`) and OCR (`index_ocr_records`) paths.
    All chunks across all docs are flattened and dense-embedded through one global
    concurrent pool (bounded by the embedder's ``CapacityLimiter``) so throughput
    scales with concurrency, not per-doc size. Does NOT create the collection.
    """
    flat_texts: list[str] = []
    for rec in records:
        rec.start = len(flat_texts)
        flat_texts.extend(c.text for c in rec.chunks)
    if not flat_texts:
        return

    # Dense: global concurrent pool. Untyped list so the None placeholders (filled
    # below) don't fight PointStruct's vector type; every slot is set before upsert.
    dense: list = [None] * len(flat_texts)  # type: ignore[type-arg]
    tok_lock = anyio.Lock()
    done = [0]

    async def embed_span(start: int) -> None:
        vecs, tok = await embedder.embed(flat_texts[start : start + embed_batch])
        for off, v in enumerate(vecs):
            dense[start + off] = v
        async with tok_lock:
            stats.embed_tokens += tok
            done[0] += len(vecs)
            if done[0] % 2000 < len(vecs):
                logger.info("embedded %d/%d chunks", done[0], len(flat_texts))

    async with anyio.create_task_group() as tg:
        for start in range(0, len(flat_texts), embed_batch):
            tg.start_soon(embed_span, start)

    # Sparse BM25 (CPU, batched in-thread).
    sparse: list[dict] = []
    for i in range(0, len(flat_texts), sparse_batch):
        sparse.extend(await bm25.encode(flat_texts[i : i + sparse_batch]))

    up_limiter = anyio.CapacityLimiter(upsert_concurrency)
    st_lock = anyio.Lock()

    def _vector(idx: int) -> dict[str, Any]:
        return {
            "dense": dense[idx],
            "sparse": models.SparseVector(
                indices=sparse[idx]["indices"], values=sparse[idx]["values"]
            ),
        }

    async def upsert_doc(rec: _DocRecord) -> None:
        points = [
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=_vector(rec.start + j),
                payload={
                    "doc_key": rec.doc_key,
                    "domain": rec.domain,
                    "page_number": rec.chunks[j].page_number,
                    "page_end": rec.chunks[j].page_end,
                    "text": rec.chunks[j].text,
                },
            )
            for j in range(len(rec.chunks))
        ]
        async with up_limiter:
            for i in range(0, len(points), upsert_batch):
                await qdrant.upsert(
                    collection_name=collection, points=points[i : i + upsert_batch]
                )
        async with st_lock:
            stats.docs += 1
            stats.total_chunks += len(rec.chunks)
            dd = stats.by_domain[rec.domain]
            dd.chunks += len(rec.chunks)
            dd.source_bytes += rec.source_bytes

    async with anyio.create_task_group() as tg:
        for rec in records:
            tg.start_soon(upsert_doc, rec)


async def retrieve(
    *,
    qdrant: AsyncQdrantClient,
    collection: str,
    query: str,
    embedder: GatewayEmbedder | OllamaEmbedder,
    bm25: Bm25Encoder,
    k: int,
    fusion: str,
    fetch: int | None = None,
    rescore: bool | None = None,
    oversampling: float | None = None,
) -> list[dict]:
    """Hybrid RRF/DBSF retrieval, returning up to ``fetch`` candidates.

    ``fetch`` defaults to ``2*k`` (the mild over-fetch the production tool uses for
    dedup). A reranker passes a larger ``fetch`` (e.g. 50) so it has a broad
    candidate pool to reorder into the top-``k`` — where a strong reranker earns
    its keep. The dense/sparse prefetch and fusion all use ``fetch``.

    ``rescore``/``oversampling`` are Qdrant quantization search params applied to
    the DENSE prefetch: rescore re-ranks the quantized candidates with the on-disk
    fp32 vectors (recall recovery), oversampling widens the quantized shortlist
    first (matters for binary). No-op on unquantized collections.
    """
    limit = fetch if fetch is not None else k * 2
    dense, _ = await embedder.embed([query])
    sparse = (await bm25.encode([query]))[0]
    fusion_enum = models.Fusion.RRF if fusion == "rrf" else models.Fusion.DBSF
    dense_params = None
    if rescore is not None or oversampling is not None:
        dense_params = models.SearchParams(
            quantization=models.QuantizationSearchParams(
                rescore=rescore, oversampling=oversampling
            )
        )
    resp = await qdrant.query_points(
        collection_name=collection,
        prefetch=[
            models.Prefetch(
                query=dense[0], using="dense", limit=limit, params=dense_params
            ),
            models.Prefetch(
                query=models.SparseVector(
                    indices=sparse["indices"], values=sparse["values"]
                ),
                using="sparse",
                limit=limit,
            ),
        ],
        query=models.FusionQuery(fusion=fusion_enum),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    out: list[dict] = []
    for point in resp.points:
        p = point.payload or {}
        out.append(
            {
                "doc_key": p.get("doc_key"),
                "domain": p.get("domain"),
                "page_number": p.get("page_number"),
                "page_end": p.get("page_end"),
                "text": p.get("text", ""),
                "score": point.score,
            }
        )
    return out


async def stream_corpus_paths(
    corpus: Path, domains: list[str] | None, limit: int | None
) -> AsyncIterator[tuple[str, str, Path]]:  # pragma: no cover - convenience only
    for item in iter_corpus(corpus, domains, limit):
        yield item
