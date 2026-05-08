"""Unit tests for SearchResult validation."""

from types import SimpleNamespace

import pytest

from nextcloud_mcp_server.search.algorithms import (
    SearchResult,
    build_search_result_from_point,
)


def _make_point(point_id, payload, score=0.5):
    """Stand-in for qdrant_client.models.ScoredPoint.

    The helper only reads ``id``, ``payload``, and ``score`` — full Pydantic
    validation isn't required for unit tests.
    """
    return SimpleNamespace(id=point_id, payload=payload, score=score)


@pytest.mark.unit
def test_search_result_rrf_score_in_range():
    """Test SearchResult accepts RRF scores in [0.0, 1.0] range."""
    result = SearchResult(
        id="1",
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
        id="1",
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
        id="1",
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
        id="1",
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
        id="1",
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
            id="1",
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
        id="1",
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
        id="1",
        doc_type="note",
        title="Test Note",
        excerpt="matching chunk text",
        score=0.9,
        chunk_start_offset=100,
        chunk_end_offset=500,
    )

    assert result.chunk_start_offset == 100
    assert result.chunk_end_offset == 500


# ---------------------------------------------------------------------------
# build_search_result_from_point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_search_result_from_point_returns_none_when_payload_missing():
    """Helper signals the caller to skip the point by returning None."""
    point = _make_point(point_id="p1", payload=None)

    assert build_search_result_from_point(point) is None


@pytest.mark.unit
def test_build_search_result_from_point_returns_none_when_doc_id_missing():
    """A payload without a doc_id key is skipped instead of raising KeyError."""
    point = _make_point(point_id="p-bad", payload={"doc_type": "note"})

    assert build_search_result_from_point(point) is None


@pytest.mark.unit
def test_build_search_result_from_point_note_payload():
    """Note-type payload populates the SearchResult fields without metadata extras."""
    point = _make_point(
        point_id="p-1",
        payload={
            "doc_id": "42",
            "doc_type": "note",
            "title": "Hello",
            "excerpt": "world",
            "chunk_start_offset": 0,
            "chunk_end_offset": 100,
            "chunk_index": 0,
            "total_chunks": 2,
        },
        score=0.91,
    )

    sr = build_search_result_from_point(point)

    assert sr is not None
    assert sr.id == "42"
    assert sr.doc_type == "note"
    assert sr.title == "Hello"
    assert sr.excerpt == "world"
    assert sr.score == pytest.approx(0.91)
    assert sr.chunk_start_offset == 0
    assert sr.chunk_end_offset == 100
    assert sr.chunk_index == 0
    assert sr.total_chunks == 2
    assert sr.point_id == "p-1"
    assert sr.metadata == {"chunk_index": 0, "total_chunks": 2}


@pytest.mark.unit
def test_build_search_result_from_point_coerces_int_doc_id_to_str():
    """Legacy int doc_id payloads are stringified defensively."""
    point = _make_point(
        point_id=1,
        payload={"doc_id": 7, "doc_type": "note"},
        score=0.5,
    )

    sr = build_search_result_from_point(point)

    assert sr is not None
    assert sr.id == "7"


@pytest.mark.unit
def test_build_search_result_from_point_file_metadata_includes_path():
    """File-type payloads with a file_path attach it under metadata['path']."""
    point = _make_point(
        point_id="p-2",
        payload={
            "doc_id": "100",
            "doc_type": "file",
            "file_path": "/Documents/report.pdf",
            "page_number": 3,
            "page_count": 12,
        },
    )

    sr = build_search_result_from_point(point)

    assert sr is not None
    assert sr.doc_type == "file"
    assert sr.metadata["path"] == "/Documents/report.pdf"
    assert sr.page_number == 3
    assert sr.page_count == 12


@pytest.mark.unit
def test_build_search_result_from_point_deck_card_metadata():
    """Deck-card payloads carry board_id/stack_id forward for verify-on-read."""
    point = _make_point(
        point_id="p-3",
        payload={
            "doc_id": "55",
            "doc_type": "deck_card",
            "board_id": 7,
            "stack_id": 12,
            "title": "Card",
        },
    )

    sr = build_search_result_from_point(point)

    assert sr is not None
    assert sr.metadata["board_id"] == 7
    assert sr.metadata["stack_id"] == 12


@pytest.mark.unit
def test_build_search_result_from_point_merges_metadata_extras():
    """metadata_extras augment the helper's computed metadata dict.

    Common fields (chunk_index, total_chunks) win over caller-supplied
    extras to keep them tied to the actual point.
    """
    point = _make_point(
        point_id="p-4",
        payload={
            "doc_id": "1",
            "doc_type": "note",
            "chunk_index": 3,
            "total_chunks": 9,
        },
    )

    sr = build_search_result_from_point(
        point,
        metadata_extras={
            "search_method": "bm25_hybrid_rrf",
            # Caller tries to override a common field — should be ignored.
            "chunk_index": "should-be-overwritten",
        },
    )

    assert sr is not None
    assert sr.metadata["search_method"] == "bm25_hybrid_rrf"
    assert sr.metadata["chunk_index"] == 3
    assert sr.metadata["total_chunks"] == 9


@pytest.mark.unit
def test_build_search_result_from_point_defaults_when_optional_fields_missing():
    """Missing optional payload keys fall back to documented defaults."""
    point = _make_point(point_id="p-5", payload={"doc_id": "1"})

    sr = build_search_result_from_point(point)

    assert sr is not None
    assert sr.doc_type == "note"  # default doc_type
    assert sr.title == "Untitled"
    assert sr.excerpt == ""
    assert sr.chunk_index == 0
    assert sr.total_chunks == 1
    assert sr.chunk_start_offset is None
    assert sr.chunk_end_offset is None
