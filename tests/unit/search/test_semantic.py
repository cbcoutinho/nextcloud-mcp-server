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


@pytest.mark.unit
async def test_dense_algorithm_rejects_document_granularity():
    """The dense-only algorithm has no grouping, so it must fail loudly rather
    than swallow the kwarg via **kwargs and silently return chunk rows to a
    caller that asked for one row per document.

    The /api/v1 layer already rejects this combination with a 422; this is the
    backstop for direct callers.
    """
    algo = SemanticSearchAlgorithm()

    with pytest.raises(ValueError, match="not supported by the dense-only"):
        await algo.search(query="hello", user_id="alice", granularity="document")


@pytest.mark.unit
def test_dense_algorithm_defaults_to_chunk_granularity():
    """Regression guard: the default must stay the value the algorithm can
    actually honour, so existing callers are unaffected."""
    import inspect

    sig = inspect.signature(SemanticSearchAlgorithm.search)

    assert sig.parameters["granularity"].default == "chunk"
