"""Unit tests for Pydantic response models."""

import pytest

from nextcloud_mcp_server.models.notes import (
    CreateNoteResponse,
    Note,
    NoteSearchResult,
    SearchNotesResponse,
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
