import logging
import time
import uuid

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


async def test_category_change_cleans_up_old_attachments_directory(
    nc_client: NextcloudClient,
):
    """
    Tests that when a note's category is changed, the old attachment directory is properly cleaned up.
    """
    note_id = None
    initial_category = "CategoryTest1"
    new_category = "CategoryTest2"
    unique_suffix = uuid.uuid4().hex[:8]
    note_title = f"Category Cleanup Test {unique_suffix}"
    attachment_filename = f"cleanup_test_{unique_suffix}.txt"
    attachment_content = f"Content for {attachment_filename}".encode("utf-8")

    try:
        # 1. Create note with initial category
        logger.info("Creating note '%s' in category '%s'", note_title, initial_category)
        created_note = await nc_client.notes.create_note(
            title=note_title, content="Initial content", category=initial_category
        )
        note_id = created_note["id"]
        etag1 = created_note["etag"]
        logger.info("Note created with ID: %s, Etag: %s", note_id, etag1)
        time.sleep(1)

        # 2. Add attachment (passing initial category)
        logger.info(
            "Adding attachment '%s' to note %s (in %s)",
            attachment_filename,
            note_id,
            initial_category,
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

        # 3. Verify attachment retrieval from initial category
        logger.info(
            "Verifying attachment retrieval from initial category '%s'",
            initial_category,
        )
        retrieved_content1, _ = await nc_client.webdav.get_note_attachment(
            note_id=note_id, filename=attachment_filename, category=initial_category
        )
        assert retrieved_content1 == attachment_content
        logger.info("Attachment retrieved successfully from initial category.")

        # 4. Construct and check the WebDAV path for the initial category's attachment directory
        initial_webdav_path = f"Notes/{initial_category}/.attachments.{note_id}"
        logger.info("Initial WebDAV path for attachments: %s", initial_webdav_path)
        # Here we would check if the directory exists, but the WebDAV client doesn't directly
        # expose directory listing functionality, so we'll infer from attachment retrieval success

        # 5. Update note category
        logger.info(
            "Updating note %s category from '%s' to '%s'",
            note_id,
            initial_category,
            new_category,
        )
        current_note_data = await nc_client.notes.get_note(note_id=note_id)
        current_etag = current_note_data["etag"]
        updated_note = await nc_client.notes.update(
            note_id=note_id,
            etag=current_etag,
            category=new_category,
            title=note_title,
            content="Updated content",
        )
        etag3 = updated_note["etag"]
        assert updated_note["category"] == new_category
        logger.info("Note category updated successfully. New Etag: %s", etag3)
        time.sleep(1)

        # 6. Verify attachment retrieval from new category
        logger.info(
            "Verifying attachment retrieval from new category '%s'", new_category
        )
        retrieved_content2, _ = await nc_client.webdav.get_note_attachment(
            note_id=note_id, filename=attachment_filename, category=new_category
        )
        assert retrieved_content2 == attachment_content
        logger.info("Attachment retrieved successfully from new category.")

        # 7. Try to retrieve from old category - this should fail
        logger.info(
            "Trying to retrieve attachment from old category '%s' - should fail",
            initial_category,
        )
        try:
            await nc_client.webdav.get_note_attachment(
                note_id=note_id, filename=attachment_filename, category=initial_category
            )
            # If we get here, it means the old directory still exists (a problem)
            logger.error(
                "ISSUE DETECTED: Was able to retrieve attachment from old category path!"
            )
            assert False, (
                "Old category attachment directory still exists and accessible!"
            )
        except HTTPStatusError as e:
            # This is the expected outcome - old directory should be gone
            logger.info(
                "Correctly got error accessing old category path: %s",
                e.response.status_code,
            )
            assert e.response.status_code == 404, (
                f"Expected 404, got {e.response.status_code}"
            )
            logger.info(
                "Verified old category attachment directory is not accessible (good!)"
            )

            # 7.1 Directly check old attachment directory existence using WebDAV PROPFIND
            logger.info(
                "Directly checking if old attachment directory exists in WebDAV"
            )
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
                if status in [
                    200,
                    207,
                ]:  # Success codes indicate the directory exists (a problem)
                    logger.error(
                        "Old attachment directory still exists! PROPFIND returned %s",
                        status,
                    )
                    assert False, (
                        f"Expected old attachment directory to be gone, but it still exists (PROPFIND returned {status})!"
                    )
                # If we got another status code (like 404), it's also good - the directory doesn't exist
                logger.info(
                    "Verified old attachment directory does not exist (PROPFIND returned %s)",
                    status,
                )
            except HTTPStatusError as e:
                # 404 is expected - directory should not exist
                assert e.response.status_code == 404, (
                    f"Expected PROPFIND to fail with 404, got {e.response.status_code}"
                )
                logger.info(
                    "Verified old attachment directory does not exist via PROPFIND (404 received)"
                )

    finally:
        # 8. Cleanup: Delete the note
        if note_id:
            logger.info("Cleaning up note ID: %s", note_id)
            try:
                await nc_client.notes.delete_note(note_id=note_id)
                logger.info("Note %s deleted.", note_id)
                time.sleep(1)

                # 9. Verify both old and new attachment paths are gone
                logger.info("Verifying all attachment paths are gone")
                with pytest.raises(HTTPStatusError) as excinfo_new:
                    await nc_client.webdav.get_note_attachment(
                        note_id=note_id,
                        filename=attachment_filename,
                        category=new_category,
                    )
                assert excinfo_new.value.response.status_code == 404

                with pytest.raises(HTTPStatusError) as excinfo_old:
                    await nc_client.webdav.get_note_attachment(
                        note_id=note_id,
                        filename=attachment_filename,
                        category=initial_category,
                    )
                assert excinfo_old.value.response.status_code == 404

                # 9.1 Directly verify directories don't exist using WebDAV PROPFIND
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
                    propfind_resp = await nc_client._client.request(
                        "PROPFIND", new_attachment_dir_path, headers=propfind_headers
                    )
                    status = propfind_resp.status_code
                    if status in [
                        200,
                        207,
                    ]:  # Success codes indicate the directory exists (a problem)
                        logger.error(
                            "New category attachment directory still exists! PROPFIND returned %s",
                            status,
                        )
                        assert False, (
                            f"Expected new category attachment directory to be gone, but it still exists (PROPFIND returned {status})!"
                        )
                    # If we got another status code (like 404), it's also good - the directory doesn't exist
                    logger.info(
                        "Verified new category attachment directory does not exist (PROPFIND returned %s)",
                        status,
                    )
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
                    propfind_resp = await nc_client._client.request(
                        "PROPFIND", old_attachment_dir_path, headers=propfind_headers
                    )
                    status = propfind_resp.status_code
                    if status in [
                        200,
                        207,
                    ]:  # Success codes indicate the directory exists (a problem)
                        logger.error(
                            "Old category attachment directory still exists! PROPFIND returned %s",
                            status,
                        )
                        assert False, (
                            f"Expected old category attachment directory to be gone, but it still exists (PROPFIND returned {status})!"
                        )
                    # If we got another status code (like 404), it's also good - the directory doesn't exist
                    logger.info(
                        "Verified old category attachment directory does not exist (PROPFIND returned %s)",
                        status,
                    )
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
                logger.error("Error during cleanup for note %s: %s", note_id, e)
