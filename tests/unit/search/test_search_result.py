"""Unit tests for SearchResult validation."""

import pytest

from nextcloud_mcp_server.search.algorithms import SearchResult


@pytest.mark.unit
def test_search_result_rrf_score_in_range():
    """Test SearchResult accepts RRF scores in [0.0, 1.0] range."""
    result = SearchResult(
        id=1,
        doc_type="note",
        title="Test Note",
        excerpt="Test excerpt",
        score=0.85,
    )

    assert result.score == 0.85


@pytest.mark.unit
def test_search_result_rrf_score_at_lower_bound():
    """Test SearchResult accepts RRF score at lower bound (0.0)."""
    result = SearchResult(
        id=1,
        doc_type="note",
        title="Test Note",
        excerpt="Test excerpt",
        score=0.0,
    )

    assert result.score == 0.0


@pytest.mark.unit
def test_search_result_rrf_score_at_upper_bound():
    """Test SearchResult accepts RRF score at upper bound (1.0)."""
    result = SearchResult(
        id=1,
        doc_type="note",
        title="Test Note",
        excerpt="Test excerpt",
        score=1.0,
    )

    assert result.score == 1.0


@pytest.mark.unit
def test_search_result_dbsf_score_above_one():
    """Test SearchResult accepts DBSF scores > 1.0.

    DBSF (Distribution-Based Score Fusion) sums normalized scores from multiple
    systems (dense semantic + sparse BM25), so scores can exceed 1.0 when both
    systems strongly agree a document is relevant.
    """
    # Typical DBSF score when both systems agree
    result = SearchResult(
        id=1,
        doc_type="note",
        title="Highly Relevant Note",
        excerpt="Contains keywords and is semantically similar",
        score=1.55,
    )

    assert result.score == 1.55


@pytest.mark.unit
def test_search_result_dbsf_score_edge_case():
    """Test SearchResult accepts DBSF maximum theoretical score (2.0).

    Maximum DBSF score with 2 systems: 1.0 (dense) + 1.0 (sparse) = 2.0
    """
    result = SearchResult(
        id=1,
        doc_type="note",
        title="Perfect Match",
        excerpt="Perfect semantic and keyword match",
        score=2.0,
    )

    assert result.score == 2.0


@pytest.mark.unit
def test_search_result_negative_score_raises_error():
    """Test SearchResult rejects negative scores."""
    with pytest.raises(ValueError) as exc_info:
        SearchResult(
            id=1,
            doc_type="note",
            title="Test Note",
            excerpt="Test excerpt",
            score=-0.1,
        )

    assert "Score must be non-negative" in str(exc_info.value)
    assert "got -0.1" in str(exc_info.value)


@pytest.mark.unit
def test_search_result_with_metadata():
    """Test SearchResult with optional metadata field."""
    result = SearchResult(
        id=1,
        doc_type="note",
        title="Test Note",
        excerpt="Test excerpt",
        score=1.25,
        metadata={"fusion_method": "dbsf", "dense_score": 0.8, "sparse_score": 0.45},
    )

    assert result.score == 1.25
    assert result.metadata["fusion_method"] == "dbsf"
    assert result.metadata["dense_score"] == 0.8
    assert result.metadata["sparse_score"] == 0.45


@pytest.mark.unit
def test_search_result_with_chunk_offsets():
    """Test SearchResult with chunk offset information."""
    result = SearchResult(
        id=1,
        doc_type="note",
        title="Test Note",
        excerpt="matching chunk text",
        score=0.9,
        chunk_start_offset=100,
        chunk_end_offset=500,
    )

    assert result.chunk_start_offset == 100
    assert result.chunk_end_offset == 500
