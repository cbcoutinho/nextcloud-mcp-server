#!/usr/bin/env python
import sys
from nextcloud_mcp_server.client import NextcloudClient

def main():
    note_id = 420  # ID of the note we created earlier
    
    # Create client
    client = NextcloudClient.from_env()
    
    # First verify the note exists
    print(f"Retrieving note {note_id}...")
    try:
        note = client.notes_get_note(note_id=note_id)
        print(f"Note retrieved: {note['title']}")
    except Exception as e:
        print(f"Error retrieving note: {e}")
        return 1
    
    # Now try to get the image attachment
    attachment_filename = "sample_image.png"
    print(f"Retrieving attachment '{attachment_filename}' from note {note_id}...")
    try:
        content, mime_type = client.get_note_attachment(
            note_id=note_id,
            filename=attachment_filename
        )
        print(f"Attachment retrieved successfully!")
        print(f"MIME type: {mime_type}")
        print(f"Content size: {len(content)} bytes")
        
        # Save the retrieved image to verify it's the same
        output_path = "retrieved_image.png"
        with open(output_path, 'wb') as f:
            f.write(content)
        print(f"Saved retrieved image to: {output_path}")
        
        return 0
    except Exception as e:
        print(f"Error retrieving attachment: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
