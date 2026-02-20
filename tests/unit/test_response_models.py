"""Unit tests for Pydantic response models."""

import pytest

from nextcloud_mcp_server.models.contacts import (
    Contact,
    ListContactsResponse,
)
from nextcloud_mcp_server.models.notes import (
    CreateNoteResponse,
    Note,
    NoteSearchResult,
    SearchNotesResponse,
)
from nextcloud_mcp_server.models.semantic import (
    SamplingSearchResponse,
    SemanticSearchResult,
)
from nextcloud_mcp_server.server.calendar import _event_dict_to_summary
from nextcloud_mcp_server.server.contacts import _raw_contact_to_model


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
            doc_type="note",
            title="Python Guide",
            category="Development",
            excerpt="Use async/await for asynchronous programming",
            score=0.92,
            chunk_index=0,
            total_chunks=3,
        ),
        SemanticSearchResult(
            id=2,
            doc_type="note",
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
            doc_type="note",
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


def _map_contact(raw: dict) -> Contact:
    """Thin wrapper around the production mapping function for test readability."""
    return _raw_contact_to_model(raw)


@pytest.mark.unit
def test_contact_mapping_preserves_email_birthday_nickname():
    """Test that list_contacts mapping preserves email, birthday, and nickname.

    Regression test for PR #574: the original mapping only kept uid, fn, etag
    and silently dropped email, birthday, and nickname.
    """
    raw_contact = {
        "vcard_id": "abc-123",
        "getetag": '"etag-val"',
        "contact": {
            "fullname": "Jane Doe",
            "email": "jane@example.com",
            "birthday": "1990-05-15",
            "nickname": "JD",
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.uid == "abc-123"
    assert contact.fn == "Jane Doe"
    assert contact.etag == '"etag-val"'
    assert contact.birthday == "1990-05-15"
    assert len(contact.emails) == 1
    assert contact.emails[0].value == "jane@example.com"
    assert contact.emails[0].type == "email"
    assert contact.custom_fields["nickname"] == "JD"


@pytest.mark.unit
def test_contact_mapping_multiple_emails():
    """Test that multiple emails are mapped correctly."""
    raw_contact = {
        "vcard_id": "def-456",
        "contact": {
            "fullname": "John Smith",
            "email": ["john@work.com", "john@home.com"],
        },
    }

    contact = _map_contact(raw_contact)

    assert len(contact.emails) == 2
    assert contact.emails[0].value == "john@work.com"
    assert contact.emails[1].value == "john@home.com"


@pytest.mark.unit
def test_contact_mapping_missing_optional_fields():
    """Test mapping when email, birthday, and nickname are absent."""
    raw_contact = {
        "vcard_id": "ghi-789",
        "contact": {"fullname": "No Details"},
    }

    contact = _map_contact(raw_contact)

    assert contact.uid == "ghi-789"
    assert contact.fn == "No Details"
    assert contact.birthday is None
    assert contact.emails == []
    assert contact.custom_fields == {}


@pytest.mark.unit
def test_list_contacts_response_wraps_contacts():
    """Test ListContactsResponse wraps contacts correctly for MCP output."""
    contacts = [
        _map_contact(
            {
                "vcard_id": "a",
                "getetag": '"e1"',
                "contact": {
                    "fullname": "Alice",
                    "email": "alice@test.com",
                    "birthday": "2000-01-01",
                    "nickname": "Ali",
                },
            }
        ),
    ]

    response = ListContactsResponse(
        contacts=contacts, addressbook="personal", total_count=1
    )

    data = response.model_dump()
    assert data["total_count"] == 1
    assert len(data["contacts"]) == 1
    c = data["contacts"][0]
    assert c["birthday"] == "2000-01-01"
    assert c["emails"][0]["value"] == "alice@test.com"
    assert c["custom_fields"]["nickname"] == "Ali"


# ============= _event_dict_to_summary tests =============


@pytest.mark.unit
def test_event_dict_to_summary_basic():
    """Test basic mapping with all fields populated."""
    event = {
        "uid": "evt-001",
        "title": "Team Standup",
        "start_datetime": "2025-07-28T09:00:00",
        "end_datetime": "2025-07-28T09:30:00",
        "all_day": False,
        "location": "Room 42",
        "description": "Daily sync",
        "categories": ["work", "meeting"],
        "status": "CONFIRMED",
        "calendar_name": "office",
        "calendar_display_name": "Office Calendar",
    }

    summary = _event_dict_to_summary(event)

    assert summary.uid == "evt-001"
    assert summary.summary == "Team Standup"
    assert summary.start == "2025-07-28T09:00:00"
    assert summary.end == "2025-07-28T09:30:00"
    assert summary.all_day is False
    assert summary.location == "Room 42"
    assert summary.description == "Daily sync"
    assert summary.categories == ["work", "meeting"]
    assert summary.status == "CONFIRMED"
    assert summary.calendar_name == "office"
    assert summary.calendar_display_name == "Office Calendar"


@pytest.mark.unit
def test_event_dict_to_summary_categories_string():
    """Test that comma-separated category string is split into a list."""
    event = {
        "uid": "evt-002",
        "title": "Review",
        "categories": "work, meeting, important",
    }

    summary = _event_dict_to_summary(event)

    assert summary.categories == ["work", "meeting", "important"]


@pytest.mark.unit
def test_event_dict_to_summary_categories_list_passthrough():
    """Test that a list of categories passes through unchanged."""
    event = {
        "uid": "evt-003",
        "title": "Review",
        "categories": ["personal", "health"],
    }

    summary = _event_dict_to_summary(event)

    assert summary.categories == ["personal", "health"]


@pytest.mark.unit
def test_event_dict_to_summary_falsy_location_description():
    """Test that empty/falsy location and description are coerced to None."""
    event = {
        "uid": "evt-004",
        "title": "Quick Chat",
        "location": "",
        "description": "",
    }

    summary = _event_dict_to_summary(event)

    assert summary.location is None
    assert summary.description is None


@pytest.mark.unit
def test_event_dict_to_summary_missing_optional_fields():
    """Test mapping with only required fields present."""
    event = {"uid": "evt-005", "title": "Minimal Event"}

    summary = _event_dict_to_summary(event)

    assert summary.uid == "evt-005"
    assert summary.summary == "Minimal Event"
    assert summary.start == ""
    assert summary.end is None
    assert summary.all_day is False
    assert summary.location is None
    assert summary.description is None
    assert summary.categories == []
    assert summary.status is None
    assert summary.calendar_name is None
    assert summary.calendar_display_name is None


@pytest.mark.unit
def test_event_dict_to_summary_calendar_name_without_display_name():
    """Test single-calendar path: calendar_name set, display_name absent."""
    event = {
        "uid": "evt-006",
        "title": "Personal Errand",
        "calendar_name": "personal",
    }

    summary = _event_dict_to_summary(event)

    assert summary.calendar_name == "personal"
    assert summary.calendar_display_name is None
