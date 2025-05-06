import pytest
import os
import time
import uuid
import logging
import tempfile
from PIL import Image, ImageDraw
from io import BytesIO
from httpx import HTTPStatusError # Import if needed for specific error checks

from nextcloud_mcp_server.client import NextcloudClient

# Note: nc_client fixture is session-scoped in conftest.py
# Note: temporary_note fixture is function-scoped in conftest.py

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

# Keep the test_image fixture as it's specific to generating image data
@pytest.fixture(scope="module") # Keep module scope if image generation is slow
def test_image_data() -> tuple[bytes, str]:
    """
    Generate test image data (bytes) and suggest a filename.
    Returns (image_bytes, suggested_filename).
    """
    logger.info("Generating test image data in memory.")
    img = Image.new('RGB', (300, 200), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(20, 20), (280, 180)], fill=(0, 120, 212)) # Blue rectangle
    draw.text((50, 90), "Nextcloud Notes Test Image", fill=(255, 255, 255)) # White text
    
    img_byte_arr = BytesIO()
    img.save(img_byte_arr, format='PNG')
    image_bytes = img_byte_arr.getvalue()
    suggested_filename = "test_image.png"
    logger.info(f"Generated test image data ({len(image_bytes)} bytes).")
    return image_bytes, suggested_filename


def test_note_with_embedded_image(nc_client: NextcloudClient, temporary_note: dict, test_image_data: tuple):
    """
    Tests creating a note, attaching an image, embedding it in the content,
    and verifying the attachment can be retrieved.
    """
    note_data = temporary_note # Use fixture for note creation/cleanup
    note_id = note_data["id"]
    note_etag = note_data["etag"]
    image_content, suggested_filename = test_image_data # Get image data from fixture

    unique_suffix = uuid.uuid4().hex[:8]
    attachment_filename = f"test_image_{unique_suffix}.png" # Make filename unique per run

    # 1. Upload the image as an attachment
    logger.info(f"Uploading image attachment '{attachment_filename}' to note {note_id}...")
    upload_response = nc_client.add_note_attachment(
        note_id=note_id,
        filename=attachment_filename,
        content=image_content,
        mime_type="image/png"
    )
    assert upload_response and upload_response.get("status_code") in [201, 204]
    logger.info(f"Image uploaded successfully (Status: {upload_response.get('status_code')}).")
    time.sleep(1) # Allow potential processing time

    # 2. Update the note content to include the embedded image references
    updated_content = f"""{note_data['content']}

## Image Embedding Test

### Markdown Syntax
![Test Image MD](.attachments.{note_id}/{attachment_filename})

### HTML Syntax
<img src=".attachments.{note_id}/{attachment_filename}" alt="Test Image HTML" width="150" />
"""
    logger.info("Updating note content with image references...")
    updated_note = nc_client.notes_update_note(
        note_id=note_id,
        etag=note_etag, # Use etag from the created note
        content=updated_content,
        title=note_data['title'], # Pass required fields
        category=note_data['category'] # Pass required fields
    )
    new_etag = updated_note["etag"]
    assert new_etag != note_etag
    logger.info("Note content updated with image references.")
    time.sleep(1)

    # 3. Verify the updated note content
    retrieved_note = nc_client.notes_get_note(note_id=note_id)
    assert f".attachments.{note_id}/{attachment_filename}" in retrieved_note["content"]
    logger.info("Verified image reference exists in updated note content.")

    # 4. Verify the image attachment can be retrieved
    logger.info(f"Retrieving image attachment '{attachment_filename}'...")
    retrieved_img_content, mime_type = nc_client.get_note_attachment(
        note_id=note_id,
        filename=attachment_filename
    )
    assert retrieved_img_content == image_content
    assert mime_type.startswith("image/png")
    logger.info("Successfully retrieved and verified image attachment content and mime type.")

    # Note cleanup is handled by the temporary_note fixture
