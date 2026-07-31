"""Unit tests for the dense-only semantic search algorithm."""

import pytest

from nextcloud_mcp_server.search.semantic import SemanticSearchAlgorithm


@pytest.mark.unit
def test_semantic_initialization_default():
    """The default threshold must stay 0.0 (no cut).

    Regression guard: this default was 0.7, inherited from the removed MCP
    sampling tool, where it silently returned zero results for questions the
    corpus answered almost verbatim. Mirrors the equivalent assertion for
    BM25HybridSearchAlgorithm in test_bm25_hybrid.py.
    """
    algo = SemanticSearchAlgorithm()

    assert algo.score_threshold == 0.0
    assert algo.name == "semantic"


@pytest.mark.unit
def test_semantic_initialization_explicit_threshold():
    """An explicitly passed threshold still wins — the API layer relies on this."""
    algo = SemanticSearchAlgorithm(score_threshold=0.5)

    assert algo.score_threshold == 0.5
    assert algo.requires_vector_db is True
