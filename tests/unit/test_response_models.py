"""Unit tests for Pydantic response models."""

import pytest

from nextcloud_mcp_server.models.notes import (
    CreateNoteResponse,
    Note,
    NoteSearchResult,
    SamplingSearchResponse,
    SearchNotesResponse,
    SemanticSearchResult,
)


@pytest.mark.unit
def test_note_model_creation():
    """Test creating a Note model with required fields."""
    note = Note(
        id=123,
        title="Test Note",
        content="# Test Content",
        modified=1700000000,
        etag="abc123",
    )

    assert note.id == 123
    assert note.title == "Test Note"
    assert note.content == "# Test Content"
    assert note.category == ""  # default value
    assert note.favorite is False  # default value
    assert note.etag == "abc123"


@pytest.mark.unit
def test_note_modified_datetime_property():
    """Test that Note.modified_datetime converts Unix timestamp correctly."""
    note = Note(
        id=1,
        title="Test",
        content="Content",
        modified=1700000000,
        etag="etag",
    )

    dt = note.modified_datetime
    assert dt.year == 2023  # Nov 14, 2023
    assert dt.month == 11


@pytest.mark.unit
def test_create_note_response_serialization():
    """Test CreateNoteResponse can serialize to JSON."""
    response = CreateNoteResponse(
        id=42,
        title="New Note",
        category="Work",
        etag="xyz789",
    )

    # Test serialization
    data = response.model_dump()
    assert data["id"] == 42
    assert data["title"] == "New Note"
    assert data["category"] == "Work"
    assert data["etag"] == "xyz789"


@pytest.mark.unit
def test_search_notes_response_wraps_results():
    """Test SearchNotesResponse wraps list of results correctly.

    This is critical - FastMCP mangles raw List[Dict] responses,
    so we must wrap them in a response model.
    """
    results = [
        NoteSearchResult(id=1, title="First Note", category="Work"),
        NoteSearchResult(id=2, title="Second Note", category="Personal"),
    ]

    response = SearchNotesResponse(
        results=results,
        query="test query",
        total_found=2,
    )

    # Verify the response structure
    assert len(response.results) == 2
    assert response.results[0].id == 1
    assert response.results[1].title == "Second Note"
    assert response.query == "test query"
    assert response.total_found == 2

    # Verify it serializes correctly
    data = response.model_dump()
    assert "results" in data
    assert isinstance(data["results"], list)
    assert len(data["results"]) == 2
    assert data["results"][0]["id"] == 1


@pytest.mark.unit
def test_note_search_result_with_score():
    """Test NoteSearchResult with optional score field."""
    result = NoteSearchResult(
        id=99,
        title="Relevant Note",
        category="Archive",
        score=0.95,
    )

    assert result.id == 99
    assert result.score == 0.95


@pytest.mark.unit
def test_note_search_result_without_score():
    """Test NoteSearchResult without optional score field."""
    result = NoteSearchResult(
        id=99,
        title="Relevant Note",
        category="Archive",
    )

    assert result.id == 99
    assert result.score is None


@pytest.mark.unit
def test_sampling_search_response_with_answer():
    """Test SamplingSearchResponse with LLM-generated answer."""
    sources = [
        SemanticSearchResult(
            id=1,
            title="Python Guide",
            category="Development",
            excerpt="Use async/await for asynchronous programming",
            score=0.92,
            chunk_index=0,
            total_chunks=3,
        ),
        SemanticSearchResult(
            id=2,
            title="Best Practices",
            category="Development",
            excerpt="Always use context managers with async operations",
            score=0.85,
            chunk_index=1,
            total_chunks=2,
        ),
    ]

    response = SamplingSearchResponse(
        query="How do I use async in Python?",
        generated_answer="Based on Document 1 and Document 2, use async/await for asynchronous programming and always use context managers.",
        sources=sources,
        total_found=2,
        search_method="semantic_sampling",
        model_used="claude-3-5-sonnet",
        stop_reason="endTurn",
        success=True,
    )

    # Verify the response structure
    assert response.query == "How do I use async in Python?"
    assert "async/await" in response.generated_answer
    assert len(response.sources) == 2
    assert response.sources[0].id == 1
    assert response.sources[0].score == 0.92
    assert response.total_found == 2
    assert response.search_method == "semantic_sampling"
    assert response.model_used == "claude-3-5-sonnet"
    assert response.stop_reason == "endTurn"
    assert response.success is True

    # Verify it serializes correctly
    data = response.model_dump()
    assert "query" in data
    assert "generated_answer" in data
    assert "sources" in data
    assert isinstance(data["sources"], list)
    assert len(data["sources"]) == 2
    assert data["sources"][0]["id"] == 1
    assert data["model_used"] == "claude-3-5-sonnet"


@pytest.mark.unit
def test_sampling_search_response_fallback():
    """Test SamplingSearchResponse when sampling fails (fallback mode)."""
    sources = [
        SemanticSearchResult(
            id=1,
            title="Note 1",
            category="Work",
            excerpt="Some content",
            score=0.75,
            chunk_index=0,
            total_chunks=1,
        )
    ]

    response = SamplingSearchResponse(
        query="test query",
        generated_answer="[Sampling unavailable: Client does not support sampling]\n\nFound 1 relevant documents. Please review the sources below.",
        sources=sources,
        total_found=1,
        search_method="semantic_sampling_fallback",
        model_used=None,
        stop_reason=None,
        success=True,
    )

    # Verify fallback behavior
    assert "[Sampling unavailable" in response.generated_answer
    assert response.search_method == "semantic_sampling_fallback"
    assert response.model_used is None
    assert response.stop_reason is None
    assert len(response.sources) == 1


@pytest.mark.unit
def test_sampling_search_response_no_results():
    """Test SamplingSearchResponse when no documents found."""
    response = SamplingSearchResponse(
        query="nonexistent topic",
        generated_answer="No relevant documents found in your Nextcloud Notes for this query.",
        sources=[],
        total_found=0,
        search_method="semantic_sampling",
        success=True,
    )

    # Verify no results case
    assert response.total_found == 0
    assert len(response.sources) == 0
    assert "No relevant documents" in response.generated_answer
    assert response.model_used is None
    assert response.stop_reason is None


@pytest.mark.unit
def test_sampling_search_response_serialization():
    """Test SamplingSearchResponse serializes to JSON correctly."""
    response = SamplingSearchResponse(
        query="test",
        generated_answer="Test answer",
        sources=[],
        total_found=0,
        search_method="semantic_sampling",
        model_used="claude-3-5-sonnet",
        stop_reason="maxTokens",
        success=True,
    )

    data = response.model_dump()

    # Check all fields are present
    assert data["query"] == "test"
    assert data["generated_answer"] == "Test answer"
    assert data["sources"] == []
    assert data["total_found"] == 0
    assert data["search_method"] == "semantic_sampling"
    assert data["model_used"] == "claude-3-5-sonnet"
    assert data["stop_reason"] == "maxTokens"
    assert data["success"] is True
