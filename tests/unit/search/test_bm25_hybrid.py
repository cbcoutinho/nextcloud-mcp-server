"""Unit tests for BM25 hybrid search algorithm."""

import pytest
from qdrant_client import models

from nextcloud_mcp_server.search.bm25_hybrid import BM25HybridSearchAlgorithm


@pytest.mark.unit
def test_bm25_hybrid_initialization_default():
    """Test BM25HybridSearchAlgorithm initializes with default RRF fusion."""
    algo = BM25HybridSearchAlgorithm()

    assert algo.score_threshold == 0.0
    assert algo.fusion == models.Fusion.RRF
    assert algo.fusion_name == "rrf"
    assert algo.name == "bm25_hybrid"


@pytest.mark.unit
def test_bm25_hybrid_initialization_with_rrf():
    """Test BM25HybridSearchAlgorithm initializes with explicit RRF fusion."""
    algo = BM25HybridSearchAlgorithm(score_threshold=0.5, fusion="rrf")

    assert algo.score_threshold == 0.5
    assert algo.fusion == models.Fusion.RRF
    assert algo.fusion_name == "rrf"


@pytest.mark.unit
def test_bm25_hybrid_initialization_with_dbsf():
    """Test BM25HybridSearchAlgorithm initializes with DBSF fusion."""
    algo = BM25HybridSearchAlgorithm(score_threshold=0.7, fusion="dbsf")

    assert algo.score_threshold == 0.7
    assert algo.fusion == models.Fusion.DBSF
    assert algo.fusion_name == "dbsf"


@pytest.mark.unit
def test_bm25_hybrid_invalid_fusion_raises_error():
    """Test BM25HybridSearchAlgorithm raises ValueError for invalid fusion."""
    with pytest.raises(ValueError) as exc_info:
        BM25HybridSearchAlgorithm(fusion="invalid")

    assert "Invalid fusion algorithm 'invalid'" in str(exc_info.value)
    assert "Must be 'rrf' or 'dbsf'" in str(exc_info.value)


@pytest.mark.unit
def test_bm25_hybrid_requires_vector_db():
    """Test BM25HybridSearchAlgorithm reports it requires vector database."""
    algo = BM25HybridSearchAlgorithm()
    assert algo.requires_vector_db is True
