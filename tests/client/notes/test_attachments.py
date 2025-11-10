import logging
import time
import uuid

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

# Note: nc_client fixture is session-scoped in conftest.py
# Note: temporary_note and temporary_note_with_attachment fixtures are function-scoped in conftest.py

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


async def test_attachments_add_and_get(
    nc_client: NextcloudClient, temporary_note_with_attachment: tuple
):
    """
    Tests adding an attachment (via fixture) and retrieving it.
    """
    note_data, attachment_filename, attachment_content = temporary_note_with_attachment
    note_id = note_data["id"]
    note_category = note_data.get("category")  # Get category from fixture data

    logger.info(
        f"Attempting to retrieve attachment '{attachment_filename}' added by fixture for note ID: {note_id}"
    )
    # Pass category to get_note_attachment
    retrieved_content, retrieved_mime = await nc_client.webdav.get_note_attachment(
        note_id=note_id, filename=attachment_filename, category=note_category
    )
    logger.info(
        f"Attachment retrieved. Mime type: {retrieved_mime}, Size: {len(retrieved_content)} bytes"
    )

    assert retrieved_content == attachment_content
    assert "text/plain" in retrieved_mime  # Fixture uses text/plain
    logger.info("Retrieved attachment content and mime type verified successfully.")


async def test_attachments_add_to_note_with_category(
    nc_client: NextcloudClient, temporary_note: dict
):
    """
    Tests adding and retrieving an attachment specifically for a note that has a category.
    Uses temporary_note fixture and adds attachment manually within the test.
    """
    note_data = (
        temporary_note  # Note created by fixture (has category 'TemporaryTesting')
    )
    note_id = note_data["id"]
    note_category = note_data["category"]
    logger.info(
        f"Using note ID: {note_id} with category '{note_category}' for attachment test."
    )

    # Add attachment within the test
    unique_suffix = uuid.uuid4().hex[:8]
    attachment_filename = f"category_attach_{unique_suffix}.txt"
    attachment_content = f"Content for {attachment_filename}".encode("utf-8")
    attachment_mime = "text/plain"

    logger.info(
        f"Attempting to add attachment '{attachment_filename}' to note ID: {note_id}"
    )
    # Pass category to add_note_attachment
    upload_response = await nc_client.webdav.add_note_attachment(
        note_id=note_id,
        filename=attachment_filename,
        content=attachment_content,
        category=note_category,  # Pass the note's category
        mime_type=attachment_mime,
    )
    assert upload_response and "status_code" in upload_response
    assert upload_response["status_code"] in [201, 204]
    logger.info(
        f"Attachment '{attachment_filename}' added successfully (Status: {upload_response['status_code']})."
    )
    time.sleep(1)

    # Get and Verify Attachment
    logger.info(
        f"Attempting to retrieve attachment '{attachment_filename}' from note ID: {note_id}"
    )
    # Pass category to get_note_attachment
    retrieved_content, retrieved_mime = await nc_client.webdav.get_note_attachment(
        note_id=note_id,
        filename=attachment_filename,
        category=note_category,  # Pass the note's category
    )
    logger.info(
        f"Attachment retrieved. Mime type: {retrieved_mime}, Size: {len(retrieved_content)} bytes"
    )

    assert retrieved_content == attachment_content
    assert attachment_mime in retrieved_mime
    logger.info(
        "Retrieved attachment content and mime type verified successfully for note with category."
    )
    # Cleanup is handled by the temporary_note fixture


async def test_attachments_cleanup_on_note_delete(
    nc_client: NextcloudClient, temporary_note_with_attachment: tuple
):
    """
    Tests that the attachment (and its directory) are deleted when the parent note is deleted.
    Relies on the cleanup mechanism within notes_delete_note and the temporary_note fixture.
    """
    note_data, attachment_filename, _ = temporary_note_with_attachment
    note_id = note_data["id"]
    note_category = note_data.get("category")  # Get category from fixture data

    # Fixture setup already added the attachment.
    # Fixture teardown (from temporary_note) will delete the note.
    # We just need to verify the attachment is gone *after* the test finishes
    # and the fixture cleanup runs. However, pytest fixtures don't easily allow
    # checking state *after* cleanup.
    # Instead, we will manually delete the note here and verify the attachment is gone.

    logger.info(
        f"Attachment '{attachment_filename}' exists for note {note_id} (added by fixture)."
    )

    # Manually delete the note
    logger.info(f"Manually deleting note ID: {note_id} within the test.")
    await nc_client.notes.delete_note(note_id=note_id)
    logger.info(f"Note ID: {note_id} deleted successfully.")
    time.sleep(1)

    # Verify Note Is Deleted
    with pytest.raises(HTTPStatusError) as excinfo_note:
        await nc_client.notes.get_note(note_id=note_id)
    assert excinfo_note.value.response.status_code == 404
    logger.info(f"Verified note {note_id} deletion (404 received).")

    # Verify Attachment Is Deleted (via 404 on GET)
    logger.info(
        f"Verifying attachment '{attachment_filename}' is deleted for note ID: {note_id}"
    )
    with pytest.raises(HTTPStatusError) as excinfo_attach:
        # Pass category to get_note_attachment - although it should fail anyway
        # because the note (and thus details) are gone.
        # The client method will raise 404 from the initial notes_get_note call.
        await nc_client.webdav.get_note_attachment(
            note_id=note_id,
            filename=attachment_filename,
            category=note_category,  # Pass category, though note fetch should fail first
        )
    # Expect 404 because the note itself is gone
    assert excinfo_attach.value.response.status_code == 404
    logger.info(
        f"Attachment '{attachment_filename}' correctly not found (404) after note deletion."
    )

    # Directly verify attachment directory doesn't exist using WebDAV PROPFIND
    logger.info("Directly verifying attachment directory doesn't exist via PROPFIND")
    webdav_base = nc_client._get_webdav_base_path()
    category_path_part = f"{note_category}/" if note_category else ""
    attachment_dir_path = (
        f"{webdav_base}/Notes/{category_path_part}.attachments.{note_id}"
    )
    propfind_headers = {"Depth": "0", "OCS-APIRequest": "true"}
    try:
        propfind_resp = await nc_client._client.request(
            "PROPFIND", attachment_dir_path, headers=propfind_headers
        )
        status = propfind_resp.status_code
        if status in [200, 207]:  # Successful PROPFIND means directory exists
            logger.error(
                f"Attachment directory still exists! PROPFIND returned {status}"
            )
            assert False, (
                f"Expected attachment directory to be gone, but PROPFIND returned {status}!"
            )
    except HTTPStatusError as e:
        assert e.response.status_code == 404, (
            f"Expected PROPFIND to fail with 404, got {e.response.status_code}"
        )
        logger.info(
            "Verified attachment directory does not exist via PROPFIND (404 received)"
        )

    # Note: The temporary_note fixture will still run its cleanup,
    # but it will find the note already deleted (404) and handle it gracefully.


async def test_attachments_category_change_handling(nc_client: NextcloudClient):
    """
    Tests attachment handling when a note's category is changed.
    Verifies attachment retrieval works before and after category change,
    and that cleanup targets the correct final location.
    """
    note_id = None
    initial_category = "CategoryA"
    new_category = "CategoryB"
    unique_suffix = uuid.uuid4().hex[:8]
    note_title = f"Category Change Test {unique_suffix}"
    attachment_filename = f"cat_change_{unique_suffix}.txt"
    attachment_content = f"Content for {attachment_filename}".encode("utf-8")

    try:
        # 1. Create note with initial category
        logger.info(f"Creating note '{note_title}' in category '{initial_category}'")
        created_note = await nc_client.notes.create_note(
            title=note_title, content="Initial content", category=initial_category
        )
        note_id = created_note["id"]
        etag1 = created_note["etag"]
        logger.info(f"Note created with ID: {note_id}, Etag: {etag1}")
        time.sleep(1)

        # 2. Add attachment (passing initial category)
        logger.info(
            f"Adding attachment '{attachment_filename}' to note {note_id} (in {initial_category})"
        )
        upload_response = await nc_client.webdav.add_note_attachment(
            note_id=note_id,
            filename=attachment_filename,
            content=attachment_content,
            category=initial_category,
            mime_type="text/plain",
        )
        assert upload_response["status_code"] in [201, 204]
        logger.info("Attachment added successfully.")
        time.sleep(1)

        # 3. Verify attachment retrieval from initial category (passing initial category)
        logger.info(
            f"Verifying attachment retrieval from initial category '{initial_category}'"
        )
        retrieved_content1, _ = await nc_client.webdav.get_note_attachment(
            note_id=note_id, filename=attachment_filename, category=initial_category
        )
        assert retrieved_content1 == attachment_content
        logger.info("Attachment retrieved successfully from initial category.")

        # 4. Update note category (with retry for ETag conflicts from background scanner)
        logger.info(
            f"Updating note {note_id} category from '{initial_category}' to '{new_category}'"
        )
        # Retry logic for 412 Precondition Failed (ETag conflict)
        # This can happen if the background vector scanner touches the note
        max_update_attempts = 3
        for attempt in range(max_update_attempts):
            try:
                # Fetch the latest etag
                current_note_data = await nc_client.notes.get_note(note_id=note_id)
                current_etag = current_note_data["etag"]
                logger.info(
                    f"Update attempt {attempt + 1}/{max_update_attempts}, current etag: {current_etag}"
                )

                updated_note = await nc_client.notes.update(
                    note_id=note_id,
                    etag=current_etag,
                    category=new_category,
                    title=note_title,
                    content="Updated content",  # Pass required fields
                )
                etag3 = updated_note["etag"]
                assert updated_note["category"] == new_category
                logger.info(f"Note category updated successfully. New Etag: {etag3}")
                break  # Success, exit retry loop

            except HTTPStatusError as e:
                if e.response.status_code == 412 and attempt < max_update_attempts - 1:
                    # ETag conflict (likely from background scanner), retry
                    logger.warning(
                        f"ETag conflict (412) on attempt {attempt + 1}, retrying..."
                    )
                    time.sleep(1)  # Brief delay before retry
                    continue
                else:
                    # Not a 412 or out of retries, re-raise
                    raise

        time.sleep(1)

        # 5. Verify attachment retrieval from *new* category (passing new category)
        logger.info(
            f"Verifying attachment retrieval from new category '{new_category}'"
        )
        retrieved_content2, _ = await nc_client.webdav.get_note_attachment(
            note_id=note_id, filename=attachment_filename, category=new_category
        )
        assert retrieved_content2 == attachment_content
        logger.info("Attachment retrieved successfully from new category.")

        # 5.1 Verify old category attachment directory is gone via WebDAV PROPFIND
        logger.info("Directly checking if old attachment directory exists in WebDAV")
        webdav_base = nc_client._get_webdav_base_path()
        old_attachment_dir_path = (
            f"{webdav_base}/Notes/{initial_category}/.attachments.{note_id}"
        )
        propfind_headers = {"Depth": "0", "OCS-APIRequest": "true"}
        try:
            propfind_resp = await nc_client._client.request(
                "PROPFIND", old_attachment_dir_path, headers=propfind_headers
            )
            status = propfind_resp.status_code
            if status in [200, 207]:  # Successful PROPFIND means directory exists
                logger.error(
                    f"Old attachment directory still exists! PROPFIND returned {status}"
                )
                assert False, (
                    f"Expected old directory to be gone, but PROPFIND returned {status} - directory still exists!"
                )
        except HTTPStatusError as e:
            assert e.response.status_code == 404, (
                f"Expected PROPFIND to fail with 404, got {e.response.status_code}"
            )
            logger.info(
                "Verified old attachment directory does not exist via PROPFIND (404 received)"
            )

        # 5.2 Verify new category attachment directory exists via WebDAV PROPFIND
        logger.info("Directly checking if new attachment directory exists in WebDAV")
        new_attachment_dir_path = (
            f"{webdav_base}/Notes/{new_category}/.attachments.{note_id}"
        )
        try:
            propfind_resp = await nc_client._client.request(
                "PROPFIND", new_attachment_dir_path, headers=propfind_headers
            )
            status = propfind_resp.status_code
            assert status in [
                207,
                200,
            ], f"Expected PROPFIND to return success (207/200), got {status}"
            logger.info(
                f"Verified new attachment directory exists via PROPFIND ({status} received)"
            )
        except HTTPStatusError as e:
            logger.error(
                f"New attachment directory not found! PROPFIND failed with {e.response.status_code}"
            )
            assert False, (
                f"Expected new attachment directory to exist, but PROPFIND failed with {e.response.status_code}"
            )

    finally:
        # 6. Cleanup: Delete the note (client should use the *final* category for cleanup path)
        if note_id:
            logger.info(
                f"Cleaning up note ID: {note_id} (last known category: '{new_category}')"
            )
            try:
                await nc_client.notes.delete_note(note_id=note_id)
                logger.info(f"Note {note_id} deleted.")
                time.sleep(1)
                # Verify note deletion
                with pytest.raises(HTTPStatusError) as excinfo_note_del:
                    await nc_client.notes.get_note(note_id=note_id)
                assert excinfo_note_del.value.response.status_code == 404
                logger.info("Verified note deleted (404).")
                # Verify attachment deletion (should fail with 404 on the initial note fetch)
                with pytest.raises(HTTPStatusError) as excinfo_attach_del:
                    # Pass the *last known* category, although the note fetch should fail first
                    await nc_client.webdav.get_note_attachment(
                        note_id=note_id,
                        filename=attachment_filename,
                        category=new_category,
                    )
                assert excinfo_attach_del.value.response.status_code == 404
                logger.info(
                    "Verified attachment cannot be retrieved after note deletion (404)."
                )

                # 6.1 Verify both old and new attachment directories are gone via WebDAV PROPFIND
                logger.info(
                    "Directly verifying attachment directories don't exist via PROPFIND"
                )
                webdav_base = nc_client._get_webdav_base_path()

                # Check new category attachment directory
                new_attachment_dir_path = (
                    f"{webdav_base}/Notes/{new_category}/.attachments.{note_id}"
                )
                propfind_headers = {"Depth": "0", "OCS-APIRequest": "true"}
                try:
                    resp = await nc_client._client.request(
                        "PROPFIND", new_attachment_dir_path, headers=propfind_headers
                    )
                    if resp.status_code in [
                        200,
                        207,
                    ]:  # Successful PROPFIND means directory exists
                        assert False, "New category attachment directory still exists!"
                except HTTPStatusError as e:
                    assert e.response.status_code == 404, (
                        f"Expected PROPFIND to fail with 404, got {e.response.status_code}"
                    )
                    logger.info(
                        "Verified new category attachment directory is gone via PROPFIND"
                    )

                # Check old category attachment directory
                old_attachment_dir_path = (
                    f"{webdav_base}/Notes/{initial_category}/.attachments.{note_id}"
                )
                try:
                    resp = await nc_client._client.request(
                        "PROPFIND", old_attachment_dir_path, headers=propfind_headers
                    )
                    if resp.status_code in [
                        200,
                        207,
                    ]:  # Successful PROPFIND means directory exists
                        assert False, "Old category attachment directory still exists!"
                except HTTPStatusError as e:
                    assert e.response.status_code == 404, (
                        f"Expected PROPFIND to fail with 404, got {e.response.status_code}"
                    )
                    logger.info(
                        "Verified old category attachment directory is gone via PROPFIND"
                    )

                logger.info(
                    "Verified all attachment directories are properly cleaned up."
                )
            except Exception as e:
                logger.error(f"Error during cleanup for note {note_id}: {e}")
