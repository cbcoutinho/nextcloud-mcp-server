#!/usr/bin/env python
import os
import sys
from nextcloud_mcp_server.client import NextcloudClient

def main():
    note_id = 487  # ID of the note we just created
    
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
        
        # Now get the current note to get its etag
        note = client.notes_get_note(note_id=note_id)
        etag = note["etag"]
        
        # Update the note content to include the image references
        updated_content = f"""# Note with Visible Image Demo

This note demonstrates how to properly embed an image in Nextcloud Notes so it's visible in the browser interface.

We'll include the sample red square image we created earlier using both Markdown and HTML methods.

## Method 1: Markdown Image Syntax
![Sample Red Square Image](.attachments.{note_id}/sample_image.png)

## Method 2: HTML Image Tag
<img src=".attachments.{note_id}/sample_image.png" alt="Sample Red Square Image" width="300" />

## Image Path Details
The image is stored at: `/Notes/.attachments.{note_id}/sample_image.png`
"""
        
        # Update the note with the references to the image
        updated_note = client.notes_update_note(
            note_id=note_id,
            etag=etag,
            content=updated_content
        )
        
        print(f"Note updated with image references. You can now view it in the browser.")
        print(f"Note URL: /index.php/apps/notes/#/note/{note_id}")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
