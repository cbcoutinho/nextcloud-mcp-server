"""Build the search algorithm a request names, for every search surface.

Shared by the HTTP search endpoints (``api/visualization.py``) and the
``nc_semantic_search`` MCP tool so the two cannot drift on what "semantic",
"bm25", "hybrid" or "exact" means.
"""

from typing import Literal

from nextcloud_mcp_server.search.algorithms import SearchAlgorithm
from nextcloud_mcp_server.search.bm25_hybrid import (
    BM25HybridSearchAlgorithm,
    search_method_label,
)
from nextcloud_mcp_server.search.exact import ExactMatchAlgorithm
from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm

SearchAlgorithmName = Literal["hybrid", "semantic", "bm25", "exact"]
FUSION_MODES = ("rrf", "dbsf")


def normalize_fusion(fusion: str | None) -> str:
    """``fusion`` when it is a known mode, else the ``rrf`` default."""
    return fusion if fusion in FUSION_MODES else "rrf"


def build_search_algorithm(
    algorithm: str, *, score_threshold: float, fusion: str
) -> SearchAlgorithm:
    """Instantiate ``algorithm`` (already validated).

    An unknown ``fusion`` raises ``ValueError`` for every algorithm, not only
    the ones that fuse: a silently ignored option reads like a ranking bug. A
    caller that wants the lenient HTTP behaviour normalizes it first
    (``normalize_fusion``).

    "hybrid" fuses dense semantic + sparse BM25 (keyword-only documents
    contribute via the sparse side of the same query). "bm25" runs the sparse
    prefetch alone and never embeds the query, so it keeps working while the
    embedding service is down. "exact" is a filtered scroll, not a ranking.
    """
    if fusion not in FUSION_MODES:
        raise ValueError(f"Invalid fusion {fusion!r}: must be 'rrf' or 'dbsf'")
    if algorithm == "exact":
        return ExactMatchAlgorithm()
    if algorithm == "semantic":
        return SemanticSearchAlgorithm(score_threshold=score_threshold)
    return BM25HybridSearchAlgorithm(
        score_threshold=score_threshold,
        fusion=fusion,
        dense=algorithm != "bm25",
    )


def search_algorithm_label(algorithm: str, fusion: str) -> str:
    """The ``algorithm`` label for search metrics and ``search_method``.

    Hybrid keeps its ``bm25_hybrid_<fusion>`` label so existing series and
    responses are unchanged; the other algorithms report their own name. The
    fusion is clamped by ``search_method_label``, so a caller-supplied string
    can never mint a Prometheus label.
    """
    if algorithm in ("semantic", "bm25", "exact"):
        return algorithm
    return search_method_label(fusion)
