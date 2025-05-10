import pytest
import logging
import time
import uuid # Keep uuid if needed for generating unique data within tests
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

# Note: nc_client fixture is now session-scoped in conftest.py

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

def test_notes_api_create_and_read(nc_client: NextcloudClient, temporary_note: dict):
    """
    Tests creating a note via the API (using fixture) and then reading it back.
    """
    created_note_data = temporary_note # Get data from fixture
    note_id = created_note_data["id"]
    
    logger.info(f"Reading note created by fixture, ID: {note_id}")
    read_note = nc_client.notes_get_note(note_id=note_id)
    
    assert read_note["id"] == note_id
    assert read_note["title"] == created_note_data["title"]
    assert read_note["content"] == created_note_data["content"]
    assert read_note["category"] == created_note_data["category"]
    logger.info(f"Successfully read and verified note ID: {note_id}")

def test_notes_api_update(nc_client: NextcloudClient, temporary_note: dict):
    """
    Tests updating a note created by the fixture.
    """
    created_note_data = temporary_note
    note_id = created_note_data["id"]
    original_etag = created_note_data["etag"]
    original_category = created_note_data["category"]

    update_title = f"Updated Title {uuid.uuid4().hex[:8]}"
    update_content = f"Updated Content {uuid.uuid4().hex[:8]}"
    
    logger.info(f"Attempting to update note ID: {note_id} with etag: {original_etag}")
    updated_note = nc_client.notes_update_note(
        note_id=note_id,
        etag=original_etag,
        title=update_title,
        content=update_content,
        # category=original_category # Explicitly pass category if required by update
    )
    logger.info(f"Note updated: {updated_note}")
    
    assert updated_note["id"] == note_id
    assert updated_note["title"] == update_title
    assert updated_note["content"] == update_content
    assert updated_note["category"] == original_category # Verify category didn't change
    assert "etag" in updated_note
    assert updated_note["etag"] != original_etag # Etag must change

    # Optional: Verify update by reading again
    time.sleep(1) # Allow potential propagation delay
    read_updated_note = nc_client.notes_get_note(note_id=note_id)
    assert read_updated_note["title"] == update_title
    assert read_updated_note["content"] == update_content
    logger.info(f"Successfully updated and verified note ID: {note_id}")

def test_notes_api_update_conflict(nc_client: NextcloudClient, temporary_note: dict):
    """
    Tests that attempting to update with an old etag fails with 412.
    """
    created_note_data = temporary_note
    note_id = created_note_data["id"]
    original_etag = created_note_data["etag"]

    # Perform a first update to change the etag
    first_update_title = f"First Update {uuid.uuid4().hex[:8]}"
    logger.info(f"Performing first update on note ID: {note_id} to change etag.")
    first_updated_note = nc_client.notes_update_note(
        note_id=note_id,
        etag=original_etag,
        title=first_update_title,
        content="First update content",
        # category=created_note_data["category"] # Pass category if required
    )
    new_etag = first_updated_note["etag"]
    assert new_etag != original_etag
    logger.info(f"Note ID: {note_id} updated, new etag: {new_etag}")
    time.sleep(1)

    # Now attempt update with the *original* etag
    logger.info(f"Attempting second update on note ID: {note_id} with OLD etag: {original_etag}")
    with pytest.raises(HTTPStatusError) as excinfo:
        nc_client.notes_update_note(
            note_id=note_id,
            etag=original_etag, # Use the stale etag
            title="This update should fail due to conflict",
            # category=created_note_data["category"] # Pass category if required
        )
    assert excinfo.value.response.status_code == 412 # Precondition Failed
    logger.info("Update with old etag correctly failed with 412 Precondition Failed.")

def test_notes_api_delete_nonexistent(nc_client: NextcloudClient):
    """
    Tests deleting a note that doesn't exist fails with 404.
    """
    non_existent_id = 999999999 # Use an ID highly unlikely to exist
    logger.info(f"\nAttempting to delete non-existent note ID: {non_existent_id}")
    with pytest.raises(HTTPStatusError) as excinfo:
        nc_client.notes_delete_note(note_id=non_existent_id)
    assert excinfo.value.response.status_code == 404
    logger.info(f"Deleting non-existent note ID: {non_existent_id} correctly failed with 404.")

# --- Attachment tests moved to test_attachments.py ---
