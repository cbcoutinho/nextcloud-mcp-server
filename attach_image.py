#!/usr/bin/env python
import os
import sys
from nextcloud_mcp_server.client import NextcloudClient

def main():
    note_id = 420  # ID of the note we created earlier
    
    # Create client
    client = NextcloudClient.from_env()
    
    # Check if image exists
    image_path = 'sample_image.png'
    if not os.path.exists(image_path):
        print(f"Error: Image file '{image_path}' not found")
        return 1
    
    # Read the image
    with open(image_path, 'rb') as f:
        image_content = f.read()
    
    print(f"Attaching image to note {note_id}...")
    try:
        # Attach the image to the note
        upload_response = client.add_note_attachment(
            note_id=note_id,
            filename="sample_image.png",
            content=image_content,
            mime_type="image/png"
        )
        
        print(f"Image attached successfully (Status: {upload_response['status_code']}).")
        print(f"Note URL: /index.php/apps/notes/#/note/{note_id}")
        return 0
    except Exception as e:
        print(f"Error attaching image: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
