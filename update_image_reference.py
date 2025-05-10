#!/usr/bin/env python
import os
import sys
from nextcloud_mcp_server.client import NextcloudClient

def main():
    note_id = 487  # ID of the note with the issue
    
    # Create client
    client = NextcloudClient.from_env()
    
    try:
        # Get the current note to get its etag
        note = client.notes_get_note(note_id=note_id)
        etag = note["etag"]
        
        # Update the note content with correct image reference syntax
        updated_content = f"""# Note with Visible Image Demo

This note demonstrates how to properly embed an image in Nextcloud Notes so it's visible in the browser interface.

We'll include the sample red square image we created earlier using both Markdown and HTML methods.

## Method 1: Markdown Image Syntax
![Sample Red Square Image](.attachments.{note_id}/sample_image.png)

## Method 2: HTML Image Tag
<img src=".attachments.{note_id}/sample_image.png" alt="Sample Red Square Image" width="300" />

## Image Path Details
The image is stored at: `/Notes/.attachments.{note_id}/sample_image.png`

## Note on Image Embedding
In Nextcloud Notes, images must be referenced with a period at the beginning of the path. The correct format is:
`.attachments.{note_id}/filename.png`

Without the leading period, the image won't display correctly.
"""
        
        # Update the note with the corrected image references
        updated_note = client.notes_update_note(
            note_id=note_id,
            etag=etag,
            content=updated_content
        )
        
        print(f"Note updated with corrected image references.")
        print(f"Note URL: /index.php/apps/notes/#/note/{note_id}")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
