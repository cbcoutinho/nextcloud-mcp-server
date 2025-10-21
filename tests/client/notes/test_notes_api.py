import asyncio
import logging
import uuid  # Keep uuid if needed for generating unique data within tests

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

# Note: nc_client fixture is now session-scoped in conftest.py

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


async def test_notes_api_create_and_read(
    nc_client: NextcloudClient, temporary_note: dict
):
    """
    Tests creating a note via the API (using fixture) and then reading it back.
    """
    created_note_data = temporary_note  # Get data from fixture
    note_id = created_note_data["id"]

    logger.info(f"Reading note created by fixture, ID: {note_id}")
    read_note = await nc_client.notes.get_note(note_id=note_id)

    assert read_note["id"] == note_id
    assert read_note["title"] == created_note_data["title"]
    assert read_note["content"] == created_note_data["content"]
    assert read_note["category"] == created_note_data["category"]
    logger.info(f"Successfully read and verified note ID: {note_id}")


async def test_notes_api_update(nc_client: NextcloudClient, temporary_note: dict):
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
    updated_note = await nc_client.notes.update(
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
    assert (
        updated_note["category"] == original_category
    )  # Verify category didn't change
    assert "etag" in updated_note
    assert updated_note["etag"] != original_etag  # Etag must change

    # Optional: Verify update by reading again
    await asyncio.sleep(1)  # Allow potential propagation delay
    read_updated_note = await nc_client.notes.get_note(note_id=note_id)
    assert read_updated_note["title"] == update_title
    assert read_updated_note["content"] == update_content
    logger.info(f"Successfully updated and verified note ID: {note_id}")


async def test_notes_api_update_conflict(
    nc_client: NextcloudClient, temporary_note: dict
):
    """
    Tests that attempting to update with an old etag fails with 412.
    """
    created_note_data = temporary_note
    note_id = created_note_data["id"]
    original_etag = created_note_data["etag"]

    # Perform a first update to change the etag
    first_update_title = f"First Update {uuid.uuid4().hex[:8]}"
    logger.info(f"Performing first update on note ID: {note_id} to change etag.")
    first_updated_note = await nc_client.notes.update(
        note_id=note_id,
        etag=original_etag,
        title=first_update_title,
        content="First update content",
        # category=created_note_data["category"] # Pass category if required
    )
    new_etag = first_updated_note["etag"]
    assert new_etag != original_etag
    logger.info(f"Note ID: {note_id} updated, new etag: {new_etag}")
    await asyncio.sleep(1)

    # Now attempt update with the *original* etag
    logger.info(
        f"Attempting second update on note ID: {note_id} with OLD etag: {original_etag}"
    )
    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.notes.update(
            note_id=note_id,
            etag=original_etag,  # Use the stale etag
            title="This update should fail due to conflict",
            # category=created_note_data["category"] # Pass category if required
        )
    assert excinfo.value.response.status_code == 412  # Precondition Failed
    logger.info("Update with old etag correctly failed with 412 Precondition Failed.")


async def test_notes_api_delete_nonexistent(nc_client: NextcloudClient):
    """
    Tests deleting a note that doesn't exist fails with 404.
    """
    non_existent_id = 999999999  # Use an ID highly unlikely to exist
    logger.info(f"\nAttempting to delete non-existent note ID: {non_existent_id}")
    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.notes.delete_note(note_id=non_existent_id)
    assert excinfo.value.response.status_code == 404
    logger.info(
        f"Deleting non-existent note ID: {non_existent_id} correctly failed with 404."
    )


async def test_notes_api_append_content_to_existing_note(
    nc_client: NextcloudClient, temporary_note: dict
):
    """
    Tests appending content to an existing note using the new append functionality.
    """
    created_note_data = temporary_note
    note_id = created_note_data["id"]
    original_content = created_note_data["content"]

    append_text = f"Appended content {uuid.uuid4().hex[:8]}"

    logger.info(f"Appending content to note ID: {note_id}")
    updated_note = await nc_client.notes.append_content(
        note_id=note_id, content=append_text
    )
    logger.info(f"Note after append: {updated_note}")

    # Verify the note was updated
    assert updated_note["id"] == note_id
    assert "etag" in updated_note
    assert updated_note["etag"] != created_note_data["etag"]  # Etag must change

    # Verify content has the separator and appended text
    expected_content = original_content + "\n---\n" + append_text
    assert updated_note["content"] == expected_content

    # Verify by reading the note again
    await asyncio.sleep(1)  # Allow potential propagation delay
    read_note = await nc_client.notes.get_note(note_id=note_id)
    assert read_note["content"] == expected_content
    logger.info(f"Successfully appended content to note ID: {note_id}")


async def test_notes_api_append_content_to_empty_note(nc_client: NextcloudClient):
    """
    Tests appending content to an empty note (no separator should be added).
    """
    # Create an empty note
    test_title = f"Empty Note {uuid.uuid4().hex[:8]}"
    test_category = "Test"

    logger.info("Creating empty note for append test")
    empty_note = await nc_client.notes.create_note(
        title=test_title,
        content="",
        category=test_category,  # Empty content
    )
    note_id = empty_note["id"]

    try:
        append_text = f"First content {uuid.uuid4().hex[:8]}"

        logger.info(f"Appending content to empty note ID: {note_id}")
        updated_note = await nc_client.notes.append_content(
            note_id=note_id, content=append_text
        )

        # For empty notes, content should just be the appended text (no separator)
        assert updated_note["content"] == append_text

        # Verify by reading the note again
        await asyncio.sleep(1)
        read_note = await nc_client.notes.get_note(note_id=note_id)
        assert read_note["content"] == append_text
        logger.info(f"Successfully appended content to empty note ID: {note_id}")

    finally:
        # Clean up the test note
        try:
            await nc_client.notes.delete_note(note_id=note_id)
            logger.info(f"Cleaned up test note ID: {note_id}")
        except Exception as e:
            logger.warning(f"Failed to clean up test note ID: {note_id}: {e}")


async def test_notes_api_append_content_multiple_times(
    nc_client: NextcloudClient, temporary_note: dict
):
    """
    Tests appending content multiple times to verify separator behavior.
    """
    created_note_data = temporary_note
    note_id = created_note_data["id"]
    original_content = created_note_data["content"]

    first_append = f"First append {uuid.uuid4().hex[:8]}"
    second_append = f"Second append {uuid.uuid4().hex[:8]}"

    logger.info(f"Performing multiple appends to note ID: {note_id}")

    # First append
    updated_note = await nc_client.notes.append_content(
        note_id=note_id, content=first_append
    )

    expected_content_after_first = original_content + "\n---\n" + first_append
    assert updated_note["content"] == expected_content_after_first

    # Second append
    updated_note = await nc_client.notes.append_content(
        note_id=note_id, content=second_append
    )

    expected_content_after_second = (
        expected_content_after_first + "\n---\n" + second_append
    )
    assert updated_note["content"] == expected_content_after_second

    # Verify by reading the note again
    await asyncio.sleep(1)
    read_note = await nc_client.notes.get_note(note_id=note_id)
    assert read_note["content"] == expected_content_after_second
    logger.info(f"Successfully performed multiple appends to note ID: {note_id}")


async def test_notes_api_append_content_nonexistent_note(nc_client: NextcloudClient):
    """
    Tests that appending to a non-existent note fails with 404.
    """
    non_existent_id = 999999999

    logger.info(f"Attempting to append to non-existent note ID: {non_existent_id}")
    with pytest.raises(HTTPStatusError) as excinfo:
        await nc_client.notes.append_content(
            note_id=non_existent_id, content="This should fail"
        )
    assert excinfo.value.response.status_code == 404
    logger.info(
        f"Appending to non-existent note ID: {non_existent_id} correctly failed with 404."
    )
