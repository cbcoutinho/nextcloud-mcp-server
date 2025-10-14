import logging
import time
import uuid
from io import BytesIO

import pytest
from httpx import HTTPStatusError  # Import if needed for specific error checks
from PIL import Image, ImageDraw

from nextcloud_mcp_server.client import NextcloudClient

# Note: nc_client fixture is session-scoped in conftest.py
# Note: temporary_note fixture is function-scoped in conftest.py

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


# Keep the test_image fixture as it's specific to generating image data
@pytest.fixture(scope="module")  # Keep module scope if image generation is slow
def test_image_data() -> tuple[bytes, str]:
    """
    Generate test image data (bytes) and suggest a filename.
    Returns (image_bytes, suggested_filename).
    """
    logger.info("Generating test image data in memory.")
    img = Image.new("RGB", (300, 200), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(20, 20), (280, 180)], fill=(0, 120, 212))  # Blue rectangle
    draw.text(
        (50, 90), "Nextcloud Notes Test Image", fill=(255, 255, 255)
    )  # White text

    img_byte_arr = BytesIO()
    img.save(img_byte_arr, format="PNG")
    image_bytes = img_byte_arr.getvalue()
    suggested_filename = "test_image.png"
    logger.info(f"Generated test image data ({len(image_bytes)} bytes).")
    return image_bytes, suggested_filename


async def test_note_with_embedded_image(
    nc_client: NextcloudClient, temporary_note: dict, test_image_data: tuple
):
    """
    Tests creating a note, attaching an image, embedding it in the content,
    and verifying the attachment can be retrieved.
    """
    note_data = temporary_note  # Use fixture for note creation/cleanup
    note_id = note_data["id"]
    note_etag = note_data["etag"]
    image_content, suggested_filename = test_image_data  # Get image data from fixture

    unique_suffix = uuid.uuid4().hex[:8]
    attachment_filename = (
        f"test_image_{unique_suffix}.png"  # Make filename unique per run
    )

    # 1. Upload the image as an attachment
    note_category = note_data.get("category")  # Get category from fixture data
    logger.info(
        f"Uploading image attachment '{attachment_filename}' to note {note_id} (category: '{note_category or ''}')..."
    )
    upload_response = await nc_client.webdav.add_note_attachment(
        note_id=note_id,
        filename=attachment_filename,
        content=image_content,
        category=note_category,  # Pass the category
        mime_type="image/png",
    )
    assert upload_response and upload_response.get("status_code") in [201, 204]
    logger.info(
        f"Image uploaded successfully (Status: {upload_response.get('status_code')})."
    )
    time.sleep(1)  # Allow potential processing time

    # 1.1 Verify attachment directory exists via WebDAV PROPFIND
    logger.info("Directly checking if attachment directory exists in WebDAV")
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
        assert status in [
            207,
            200,
        ], f"Expected PROPFIND to return success (207/200), got {status}"
        logger.info(
            f"Verified attachment directory exists via PROPFIND ({status} received)"
        )
    except HTTPStatusError as e:
        logger.error(
            f"Attachment directory not found! PROPFIND failed with {e.response.status_code}"
        )
        assert False, (
            f"Expected attachment directory to exist, but PROPFIND failed with {e.response.status_code}"
        )

    # 2. Update the note content to include the embedded image references
    updated_content = f"""{note_data["content"]}

## Image Embedding Test

### Markdown Syntax
![Test Image MD](.attachments.{note_id}/{attachment_filename})

### HTML Syntax
<img src=".attachments.{note_id}/{attachment_filename}" alt="Test Image HTML" width="150" />
"""
    logger.info("Updating note content with image references...")
    updated_note = await nc_client.notes.update(
        note_id=note_id,
        etag=note_etag,  # Use etag from the created note
        content=updated_content,
        title=note_data["title"],  # Pass required fields
        category=note_data["category"],  # Pass required fields
    )
    new_etag = updated_note["etag"]
    assert new_etag != note_etag
    logger.info("Note content updated with image references.")
    time.sleep(1)

    # 3. Verify the updated note content
    retrieved_note = await nc_client.notes.get_note(note_id=note_id)
    assert f".attachments.{note_id}/{attachment_filename}" in retrieved_note["content"]
    logger.info("Verified image reference exists in updated note content.")

    # 4. Verify the image attachment can be retrieved
    logger.info(
        f"Retrieving image attachment '{attachment_filename}' (category: '{note_category or ''}')..."
    )
    # Pass category to get_note_attachment
    retrieved_img_content, mime_type = await nc_client.webdav.get_note_attachment(
        note_id=note_id, filename=attachment_filename, category=note_category
    )
    assert retrieved_img_content == image_content
    assert mime_type.startswith("image/png")
    logger.info(
        "Successfully retrieved and verified image attachment content and mime type."
    )

    # 5. Manually trigger deletion to verify cleanup (instead of waiting for fixture teardown)
    logger.info(
        f"Manually deleting note ID: {note_id} to verify proper attachment cleanup"
    )
    await nc_client.notes.delete_note(note_id=note_id)
    logger.info(f"Note ID: {note_id} deleted successfully.")
    time.sleep(1)

    # 6. Verify note is deleted
    with pytest.raises(HTTPStatusError) as excinfo_note:
        await nc_client.notes.get_note(note_id=note_id)
    assert excinfo_note.value.response.status_code == 404
    logger.info(f"Verified note {note_id} deletion (404 received).")

    # 7. Verify attachment directory is deleted via WebDAV PROPFIND
    logger.info("Directly verifying attachment directory doesn't exist via PROPFIND")
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
