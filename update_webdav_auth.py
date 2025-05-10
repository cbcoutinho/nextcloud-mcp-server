#!/usr/bin/env python
import sys
import os
import base64
from nextcloud_mcp_server.client import NextcloudClient, HTTPStatusError
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_webdav_auth_with_attachment():
    """
    Test function to verify WebDAV authentication by attempting to use add_note_attachment.
    """
    client = NextcloudClient.from_env()
    print("Client authentication type:", type(client._client.auth).__name__)
    
    username = os.environ["NEXTCLOUD_USERNAME"]
    webdav_base = client._get_webdav_base_path()
    notes_path = f"{webdav_base}/Notes"
    print(f"Target WebDAV Notes path for PROPFIND check: {notes_path}")

    temp_note_id = None
    try:
        # 1. Create a temporary note to get a note_id
        print("\nCreating a temporary note...")
        temp_note_title = f"Temp Note for WebDAV Test - {int(time.time())}"
        created_note = client.notes_create_note(title=temp_note_title, content="Test content")
        temp_note_id = created_note.get("id")
        if not temp_note_id:
            print("Error: Failed to create temporary note.")
            return 1
        print(f"Temporary note created with ID: {temp_note_id}")

        # 2. Attempt to add an attachment (this will trigger the internal PROPFIND)
        print(f"\nTest: Attempting add_note_attachment for note_id {temp_note_id} (uses client's BasicAuth)")
        dummy_content = b"This is a test attachment."
        dummy_filename = "test_attachment.txt"
        
        # The add_note_attachment method itself contains the PROPFIND check
        # and will log details if it fails.
        response_data = client.add_note_attachment(
            note_id=temp_note_id,
            filename=dummy_filename,
            content=dummy_content,
            mime_type="text/plain"
        )
        print(f"add_note_attachment response: {response_data}")
        if response_data and response_data.get("status_code") in [201, 204]:
             print("Success! add_note_attachment (and its internal PROPFIND) worked.")
        else:
            print("Failure or unexpected response from add_note_attachment.")
            # The client.py logs should show details of the PROPFIND if it failed.

    except HTTPStatusError as e:
        print(f"HTTPStatusError during add_note_attachment: {e.response.status_code} - {e.response.text}")
        if e.response.status_code == 401:
            print("Reproduced 401 Unauthorized during add_note_attachment's PROPFIND check!")
        else:
            print("An HTTP error other than 401 occurred.")
    except Exception as e:
        print(f"An unexpected error occurred: {str(e)}")
    finally:
        # 3. Clean up: Delete the temporary note
        if temp_note_id:
            print(f"\nCleaning up: Deleting temporary note ID {temp_note_id}...")
            try:
                client.notes_delete_note(note_id=temp_note_id)
                print(f"Successfully deleted temporary note ID {temp_note_id}.")
            except Exception as e_del:
                print(f"Error deleting temporary note ID {temp_note_id}: {str(e_del)}")
    
    return 0

if __name__ == "__main__":
    sys.exit(test_webdav_auth_with_attachment())
