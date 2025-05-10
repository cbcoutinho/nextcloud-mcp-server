#!/usr/bin/env python
import sys
import time
from nextcloud_mcp_server.client import NextcloudClient

def main():
    # Create client
    client = NextcloudClient.from_env()
    
    # 1. Create a new test note
    test_title = "Test Note for Deletion with Attachment"
    print(f"Creating test note: {test_title}...")
    note = client.notes_create_note(
        title=test_title,
        content="This note will be deleted but its attachment should remain.",
        category="Test"
    )
    note_id = note["id"]
    print(f"Note created with ID: {note_id}")
    
    # 2. Attach the existing image to the note
    print(f"Attaching image to note {note_id}...")
    with open("sample_image.png", 'rb') as f:
        image_content = f.read()
    
    upload_response = client.add_note_attachment(
        note_id=note_id,
        filename="deletion_test_image.png",
        content=image_content,
        mime_type="image/png"
    )
    print(f"Image attached successfully (Status: {upload_response['status_code']}).")
    
    # 3. Verify the attachment exists
    print(f"Verifying attachment exists...")
    content, mime_type = client.get_note_attachment(
        note_id=note_id,
        filename="deletion_test_image.png"
    )
    print(f"Attachment verified (Size: {len(content)} bytes)")
    
    # 4. Delete the note
    print(f"\nDeleting note {note_id}...")
    response = client.notes_delete_note(note_id=note_id)
    print(f"Note deleted successfully.")
    
    # Wait a moment for deletion to process
    time.sleep(1)
    
    # 5. Verify the note is gone
    print("\nVerifying note is deleted...")
    try:
        client.notes_get_note(note_id=note_id)
        print("ERROR: Note still exists!")
        return 1
    except Exception as e:
        print(f"Note confirmed deleted (404 Not Found expected): {e}")
    
    # 6. Check if attachment still exists (expected behavior)
    print("\nChecking if attachment still exists (orphaned)...")
    try:
        content, mime_type = client.get_note_attachment(
            note_id=note_id,
            filename="deletion_test_image.png"
        )
        print("EXPECTED BEHAVIOR: Attachment still exists after note deletion!")
        print(f"Attachment size: {len(content)} bytes")
        print(f"This matches the documented behavior of Nextcloud Notes.")
        
        # Save the orphaned attachment to verify
        output_path = "orphaned_attachment.png"
        with open(output_path, 'wb') as f:
            f.write(content)
        print(f"Saved orphaned attachment to: {output_path}")
        
        return 0
    except Exception as e:
        print(f"Unexpected: Attachment was deleted with note: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
