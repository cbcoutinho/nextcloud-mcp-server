import logging

import httpx
import pytest

from nextcloud_mcp_server.client.notes import NotesClient
from tests.client.conftest import create_mock_error_response, create_mock_note_response

logger = logging.getLogger(__name__)

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


async def test_notes_api_get_note(mocker):
    """Test that get_note correctly parses the API response."""
    # Create mock response
    mock_response = create_mock_note_response(
        note_id=123,
        title="Test Note",
        content="Test content",
        category="Test",
        etag="abc123",
    )

    # Mock the _make_request method
    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NotesClient, "_make_request", return_value=mock_response
    )

    # Create client and test
    client = NotesClient(mock_client, "testuser")
    note = await client.get_note(note_id=123)

    # Verify the response was parsed correctly
    assert note["id"] == 123
    assert note["title"] == "Test Note"
    assert note["content"] == "Test content"
    assert note["category"] == "Test"
    assert note["etag"] == "abc123"

    # Verify the correct API endpoint was called
    mock_make_request.assert_called_once_with("GET", "/apps/notes/api/v1/notes/123")


async def test_notes_api_create_note(mocker):
    """Test that create_note correctly parses the API response."""
    mock_response = create_mock_note_response(
        note_id=456,
        title="New Note",
        content="New content",
        category="Category",
        etag="def456",
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        NotesClient, "_make_request", return_value=mock_response
    )

    client = NotesClient(mock_client, "testuser")
    note = await client.create_note(
        title="New Note", content="New content", category="Category"
    )

    assert note["id"] == 456
    assert note["title"] == "New Note"
    assert note["content"] == "New content"
    assert note["category"] == "Category"

    # Verify the correct API call was made
    mock_make_request.assert_called_once_with(
        "POST",
        "/apps/notes/api/v1/notes",
        json={"title": "New Note", "content": "New content", "category": "Category"},
    )


async def test_notes_api_update(mocker):
    """Test that update correctly parses the API response and handles etag."""
    # Mock the update response (no category passed, so no GET call happens)
    update_response = create_mock_note_response(
        note_id=123,
        title="Updated Title",
        content="Updated content",
        category="Test",
        etag="new_etag",
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)

    # Mock _make_request to return the update response
    mock_make_request = mocker.patch.object(NotesClient, "_make_request")
    mock_make_request.return_value = update_response

    client = NotesClient(mock_client, "testuser")
    updated_note = await client.update(
        note_id=123,
        etag="abc123",
        title="Updated Title",
        content="Updated content",
    )

    assert updated_note["id"] == 123
    assert updated_note["title"] == "Updated Title"
    assert updated_note["content"] == "Updated content"
    assert updated_note["etag"] == "new_etag"

    # Verify the PUT request was made with the correct etag header (only 1 call since no category)
    assert mock_make_request.call_count == 1
    put_call = mock_make_request.call_args_list[0]
    assert put_call[0] == ("PUT", "/apps/notes/api/v1/notes/123")
    assert put_call[1]["headers"]["If-Match"] == '"abc123"'


async def test_notes_api_update_conflict(mocker):
    """Test that update raises HTTPStatusError on 412 conflict."""
    # Mock the 412 error response
    error_response = create_mock_error_response(412, "Precondition Failed")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NotesClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "412 Precondition Failed",
        request=httpx.Request("PUT", "http://test.local"),
        response=error_response,
    )

    client = NotesClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.update(
            note_id=123,
            etag="old_etag",
            title="This should fail",
        )

    assert excinfo.value.response.status_code == 412


async def test_notes_api_delete_note(mocker):
    """Test that delete_note makes the correct API call."""
    # Mock get_note response (to fetch category for cleanup)
    get_response = create_mock_note_response(note_id=123, category="Test")

    # Mock delete response
    delete_response = create_mock_note_response(note_id=123)

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NotesClient, "_make_request")
    mock_make_request.side_effect = [get_response, delete_response]

    client = NotesClient(mock_client, "testuser")
    await client.delete_note(note_id=123)

    # Verify DELETE was called
    assert any(call[0][0] == "DELETE" for call in mock_make_request.call_args_list)


async def test_notes_api_delete_nonexistent(mocker):
    """Test that deleting a non-existent note raises 404."""
    # Mock 404 error when fetching note details
    error_response = create_mock_error_response(404, "Not Found")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NotesClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("GET", "http://test.local"),
        response=error_response,
    )

    client = NotesClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.delete_note(note_id=999999999)

    assert excinfo.value.response.status_code == 404


async def test_notes_api_append_content(mocker):
    """Test that append_content correctly appends to existing content."""
    # Mock get_note response (to fetch current content)
    get_response = create_mock_note_response(
        note_id=123,
        content="Original content",
        etag="old_etag",
    )

    # Mock update response with appended content
    update_response = create_mock_note_response(
        note_id=123,
        content="Original content\n---\nAppended content",
        etag="new_etag",
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NotesClient, "_make_request")
    # First call: GET (from get_note), second call: PUT (from update)
    mock_make_request.side_effect = [get_response, update_response]

    client = NotesClient(mock_client, "testuser")
    updated_note = await client.append_content(note_id=123, content="Appended content")

    assert updated_note["content"] == "Original content\n---\nAppended content"
    assert updated_note["etag"] == "new_etag"


async def test_notes_api_append_content_to_empty_note(mocker):
    """Test that appending to empty note doesn't add separator."""
    # Mock get_note response with empty content
    get_response = create_mock_note_response(
        note_id=123,
        content="",
        etag="old_etag",
    )

    # Mock update response with just the appended text (no separator)
    update_response = create_mock_note_response(
        note_id=123,
        content="First content",
        etag="new_etag",
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NotesClient, "_make_request")
    # First call: GET (from get_note), second call: PUT (from update)
    mock_make_request.side_effect = [get_response, update_response]

    client = NotesClient(mock_client, "testuser")
    updated_note = await client.append_content(note_id=123, content="First content")

    # For empty notes, no separator should be added
    assert updated_note["content"] == "First content"


async def test_notes_api_append_content_nonexistent_note(mocker):
    """Test that appending to a non-existent note raises 404."""
    error_response = create_mock_error_response(404, "Not Found")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(NotesClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("GET", "http://test.local"),
        response=error_response,
    )

    client = NotesClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.append_content(note_id=999999999, content="This should fail")

    assert excinfo.value.response.status_code == 404
