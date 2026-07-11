#!/usr/bin/env python3
"""Offline chunk-density sweep for chunker-config A/B (Deck #636 / #626).

Measures **chunks/MB** — the dense-vector density that drives the hybrid storage
cost-to-serve — for candidate chunker configs, WITHOUT re-indexing any tenant or
touching billing. Use it to pick a config before the fleet-wide re-index +
storage-rate re-calibration (the live re-measure in note 389935 is the
authoritative fleet number; this is the fast pre-flight).

Density is computed with the SAME formula as the production metric
``record_chunk_density`` (``nextcloud_mcp_server/observability/metrics.py``):

    chunks_per_mb = chunk_count / (source_bytes / 1_000_000)   # decimal MB

``source_bytes`` is the raw PDF byte size, matching ``ingested_byte_size(...)``
for files (the raw WebDAV binary size the histogram observes).

Corpus: OHR-Bench (``~/Downloads/OHR-Bench``, 1,261 PDFs across academic /
administration / finance / law / manual / news / textbook — the same docs live
in the smoke-test tenant). QA pairs for a retrieval-rank quality check live in
``~/Software/OHR-Bench/data/qas.json``; the existing A/B retrieval harness under
``~/Software/OHR-Bench/astrolabe/`` (+ ``ocr_bench/ab_run.py``) drives retrieval
against a running stack. This script deliberately covers only the density half —
the pricing-relevant number — and leaves retrieval quality to that harness.

Reuses repo primitives only (no network): ``PyMuPDFProcessor`` for extraction →
``page_boundaries``, then ``DocumentChunker`` / ``PageAwareChunker`` (incl. the
``pack_pages`` variant).

Examples
--------
    # Quick look: 15 PDFs/category, default configs, table output
    uv run python -m scripts.chunk_density_sweep --limit 15

    # Full sweep to JSON for the pricing re-calibration hand-off
    uv run python -m scripts.chunk_density_sweep --output sweep.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import anyio

# Tier-1 fast extractor (pypdfium2). Used directly — not via the registry — so
# the sweep skips the subprocess OOM-guard isolation that needs a running-service
# context. Matches the production born-digital path AND OHR-Bench's
# ``retrieval_base/pypdfium2_fast``.
from nextcloud_mcp_server.document_processors.pypdfium2_fast import _extract
from nextcloud_mcp_server.vector.document_chunker import (
    DocumentChunker,
    PageAwareChunker,
)

DEFAULT_CORPUS = Path("~/Downloads/OHR-Bench").expanduser()
DEFAULT_CHUNK_SIZES = (2048, 4096)


def _safe_output_path(raw: str) -> Path:
    """Resolve a user-supplied ``--output`` path, confined to the working tree.

    The report is dev output, but the path comes from a CLI argument, so refuse
    anything that resolves outside the current working directory (defends against
    ``../`` traversal / absolute-path escape when the script is driven by an
    agent or wrapper).
    """
    base = Path.cwd().resolve()
    resolved = (base / raw).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(
            f"--output must stay within {base}, refusing to write {resolved}"
        )
    return resolved


@dataclass(frozen=True)
class ChunkerConfig:
    """A named chunker configuration under test."""

    name: str
    chunk_size: int
    page_aware: bool
    pack_pages: bool

    async def chunk_count(self, content: str, page_boundaries: list[dict]) -> int:
        """Number of chunks this config produces for one document."""
        if self.page_aware and page_boundaries:
            chunks = await PageAwareChunker(
                chunk_size=self.chunk_size, pack_pages=self.pack_pages
            ).chunk_text(content, page_boundaries)
        else:
            chunks = await DocumentChunker(chunk_size=self.chunk_size).chunk_text(
                content
            )
        return len(chunks)


def build_configs(chunk_sizes: tuple[int, ...]) -> list[ChunkerConfig]:
    """The sweep matrix: {page-aware, page-aware+pack} × chunk_size.

    ``page-aware`` (pack off) is today's production baseline; ``+pack`` is the
    Deck #636 density fix. A larger chunk_size multiplies the pack win.
    """
    configs: list[ChunkerConfig] = []
    for cs in chunk_sizes:
        configs.append(
            ChunkerConfig(f"page-aware@{cs}", cs, page_aware=True, pack_pages=False)
        )
        configs.append(
            ChunkerConfig(f"page-pack@{cs}", cs, page_aware=True, pack_pages=True)
        )
    return configs


@dataclass
class ConfigStats:
    """Accumulated per-config chunks/MB samples (one per document)."""

    densities: list[float] = field(default_factory=list)
    total_chunks: int = 0
    total_bytes: int = 0

    def add(self, chunks: int, source_bytes: int) -> None:
        if source_bytes > 0 and chunks > 0:
            self.densities.append(chunks / (source_bytes / 1_000_000))
            self.total_chunks += chunks
            self.total_bytes += source_bytes

    def summary(self) -> dict[str, float]:
        if not self.densities:
            return {"n": 0}
        ds = sorted(self.densities)
        return {
            "n": len(ds),
            "mean": round(statistics.fmean(ds), 1),
            "p50": round(_pct(ds, 0.50), 1),
            "p90": round(_pct(ds, 0.90), 1),
            "p99": round(_pct(ds, 0.99), 1),
            # Byte-weighted density (matches the live indexed_chunks/bytes_processed
            # cross-check in note 389935 — diverges from doc-mean on mixed corpora).
            "byte_weighted": round(
                self.total_chunks / (self.total_bytes / 1_000_000), 1
            ),
            # Implied dense-RAM carry €/GiB-mo (6,144 B/point × €1.75/GB-mo).
            "eur_per_gib_mo": round(statistics.fmean(ds) * 0.01101, 2),
        }


def _pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile (matches the histogram's coarse buckets well enough)."""
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def iter_pdfs(
    corpus: Path, categories: list[str] | None, limit: int | None
) -> Iterator[tuple[str, Path]]:
    """Yield (category, pdf_path) for PDFs under the corpus, capped per category."""
    cats = categories or sorted(p.name for p in corpus.iterdir() if p.is_dir())
    for cat in cats:
        cat_dir = corpus / cat
        if not cat_dir.is_dir():
            continue
        pdfs = sorted(cat_dir.rglob("*.pdf"))
        if limit is not None:
            pdfs = pdfs[:limit]
        for pdf in pdfs:
            yield cat, pdf


async def run(args: argparse.Namespace) -> dict:
    corpus = Path(args.corpus).expanduser()
    if not corpus.is_dir():
        raise SystemExit(f"corpus not found: {corpus}")

    configs = build_configs(tuple(args.chunk_sizes))
    # category -> config-name -> ConfigStats
    stats: dict[str, dict[str, ConfigStats]] = {}
    overall: dict[str, ConfigStats] = {c.name: ConfigStats() for c in configs}
    processed = 0
    skipped = 0

    for category, pdf in iter_pdfs(corpus, args.categories, args.limit):
        try:
            pdf_bytes = pdf.read_bytes()
            text, metadata = await anyio.to_thread.run_sync(  # type: ignore[attr-defined]
                _extract, pdf_bytes
            )
        except Exception as exc:  # noqa: BLE001 — a bad PDF must not abort the sweep
            skipped += 1
            if args.verbose:
                print(f"  skip {pdf.name}: {exc}")
            continue
        if not text:
            skipped += 1
            continue

        source_bytes = len(pdf_bytes)  # raw binary size — matches the live metric
        boundaries = metadata.get("page_boundaries") or []
        cat_stats = stats.setdefault(category, {c.name: ConfigStats() for c in configs})
        for cfg in configs:
            n = await cfg.chunk_count(text, boundaries)
            cat_stats[cfg.name].add(n, source_bytes)
            overall[cfg.name].add(n, source_bytes)
        processed += 1
        if args.verbose and processed % 25 == 0:
            print(f"  ...{processed} PDFs processed")

    report = {
        "corpus": str(corpus),
        "processed": processed,
        "skipped": skipped,
        "configs": [c.name for c in configs],
        "per_category": {
            cat: {name: s.summary() for name, s in by_cfg.items()}
            for cat, by_cfg in stats.items()
        },
        "overall": {name: s.summary() for name, s in overall.items()},
    }
    return report


def print_table(report: dict) -> None:
    print(
        f"\nChunk-density sweep — {report['processed']} PDFs "
        f"({report['skipped']} skipped) from {report['corpus']}\n"
    )
    header = f"{'config':<18}{'n':>6}{'mean':>8}{'p50':>7}{'p90':>7}{'p99':>7}{'byteW':>8}{'€/GiB':>8}"
    for scope, block in (
        ("OVERALL", report["overall"]),
        *report["per_category"].items(),
    ):
        print(f"— {scope} —")
        print(header)
        for name, s in block.items():
            if s.get("n"):
                print(
                    f"{name:<18}{s['n']:>6}{s['mean']:>8}{s['p50']:>7}{s['p90']:>7}"
                    f"{s['p99']:>7}{s['byte_weighted']:>8}{s['eur_per_gib_mo']:>8}"
                )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument(
        "--categories",
        nargs="*",
        default=None,
        help="Subset of category dirs (default: all under corpus)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max PDFs per category (default: all)",
    )
    parser.add_argument(
        "--chunk-sizes",
        type=int,
        nargs="+",
        default=list(DEFAULT_CHUNK_SIZES),
        dest="chunk_sizes",
    )
    parser.add_argument("--output", default=None, help="Write full report JSON here")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    report = anyio.run(run, args)
    print_table(report)
    if args.output:
        out_path = _safe_output_path(args.output)
        out_path.write_text(json.dumps(report, indent=2))
        print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
