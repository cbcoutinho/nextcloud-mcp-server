import pytest
import os
import logging
import uuid
import time
from nextcloud_mcp_server.client import NextcloudClient, HTTPStatusError

logger = logging.getLogger(__name__)

@pytest.fixture(scope="session")
def nc_client() -> NextcloudClient:
    """
    Fixture to create a NextcloudClient instance for integration tests.
    Uses environment variables for configuration.
    """
    assert os.getenv("NEXTCLOUD_HOST"), "NEXTCLOUD_HOST env var not set"
    assert os.getenv("NEXTCLOUD_USERNAME"), "NEXTCLOUD_USERNAME env var not set"
    assert os.getenv("NEXTCLOUD_PASSWORD"), "NEXTCLOUD_PASSWORD env var not set"
    logger.info("Creating session-scoped NextcloudClient from environment variables.")
    client = NextcloudClient.from_env()
    # Optional: Perform a quick check like getting capabilities to ensure connection works
    try:
        client.capabilities()
        logger.info("NextcloudClient session fixture initialized and capabilities checked.")
    except Exception as e:
        logger.error(f"Failed to initialize NextcloudClient session fixture: {e}")
        pytest.fail(f"Failed to connect to Nextcloud or get capabilities: {e}")
    return client

@pytest.fixture
def temporary_note(nc_client: NextcloudClient):
    """
    Fixture to create a temporary note for a test and ensure its deletion afterward.
    Yields the created note dictionary.
    """
    note_id = None
    unique_suffix = uuid.uuid4().hex[:8]
    note_title = f"Temporary Test Note {unique_suffix}"
    note_content = f"Content for temporary note {unique_suffix}"
    note_category = "TemporaryTesting"
    created_note_data = None

    logger.info(f"Creating temporary note: {note_title}")
    try:
        created_note_data = nc_client.notes_create_note(
            title=note_title, content=note_content, category=note_category
        )
        note_id = created_note_data.get("id")
        if not note_id:
            pytest.fail("Failed to get ID from created temporary note.")
        
        logger.info(f"Temporary note created with ID: {note_id}")
        yield created_note_data # Provide the created note data to the test

    finally:
        if note_id:
            logger.info(f"Cleaning up temporary note ID: {note_id}")
            try:
                nc_client.notes_delete_note(note_id=note_id)
                logger.info(f"Successfully deleted temporary note ID: {note_id}")
            except HTTPStatusError as e:
                # Ignore 404 if note was already deleted by the test itself
                if e.response.status_code != 404:
                    logger.error(f"HTTP error deleting temporary note {note_id}: {e}")
                else:
                    logger.warning(f"Temporary note {note_id} already deleted (404).")
            except Exception as e:
                logger.error(f"Unexpected error deleting temporary note {note_id}: {e}")

@pytest.fixture
def temporary_note_with_attachment(nc_client: NextcloudClient, temporary_note: dict):
    """
    Fixture that creates a temporary note, adds an attachment, and cleans up both.
    Yields a tuple: (note_data, attachment_filename, attachment_content).
    Depends on the temporary_note fixture.
    """
    note_data = temporary_note
    note_id = note_data["id"]
    note_category = note_data.get("category") # Get category from the note data
    unique_suffix = uuid.uuid4().hex[:8]
    attachment_filename = f"temp_attach_{unique_suffix}.txt"
    attachment_content = f"Content for {attachment_filename}".encode('utf-8')
    attachment_mime = "text/plain"
    
    logger.info(f"Adding attachment '{attachment_filename}' to temporary note ID: {note_id} (category: '{note_category or ''}')")
    try:
        # Pass the category to add_note_attachment
        upload_response = nc_client.add_note_attachment(
            note_id=note_id,
            filename=attachment_filename,
            content=attachment_content,
            category=note_category, # Pass the fetched category
            mime_type=attachment_mime
        )
        assert upload_response.get("status_code") in [201, 204], f"Failed to upload attachment: {upload_response}"
        logger.info(f"Attachment '{attachment_filename}' added successfully.")
        
        yield note_data, attachment_filename, attachment_content
        
        # Cleanup for the attachment is handled by the notes_delete_note call
        # in the temporary_note fixture's finally block (which deletes the .attachments dir)

    except Exception as e:
        logger.error(f"Failed to add attachment in fixture: {e}")
        pytest.fail(f"Fixture setup failed during attachment upload: {e}")

    # Note: The temporary_note fixture's finally block will handle note deletion,
    # which should also trigger the WebDAV directory deletion attempt.
