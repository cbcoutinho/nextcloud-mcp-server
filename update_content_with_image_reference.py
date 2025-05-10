#!/usr/bin/env python
import sys
from nextcloud_mcp_server.client import NextcloudClient

def main():
    note_id = 420  # ID of the note with the image attachment
    
    # Create client
    client = NextcloudClient.from_env()
    
    # First get the current note
    try:
        note = client.notes_get_note(note_id=note_id)
        print(f"Retrieved note: {note['title']}")
        
        # Update the note content to include a direct reference to the image
        updated_content = f"""# Note with Image Attachment

This note demonstrates attaching images to Nextcloud Notes.

An image will be attached to this note as a demonstration.

## Image Reference

The image is attached but not displayed inline in the Notes UI. 
Attachments in Nextcloud Notes exist as separate files in the .attachments.{note_id} 
directory but aren't automatically embedded in the note content.

You can view the image by going to the Files app and navigating to:
/Notes/.attachments.{note_id}/sample_image.png

## Orphaned Attachments

When notes are deleted, their attachments remain in the system.
This is the expected behavior of the official Nextcloud Notes app.
"""
        
        # Update the note with the new content
        updated_note = client.notes_update_note(
            note_id=note_id,
            etag=note['etag'],
            content=updated_content
        )
        
        print(f"Note updated successfully with image reference information.")
        return 0
        
    except Exception as e:
        print(f"Error updating note: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
