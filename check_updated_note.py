#!/usr/bin/env python
import sys
from nextcloud_mcp_server.client import NextcloudClient

def main():
    note_id = 420  # ID of the note with the image attachment
    
    # Create client
    client = NextcloudClient.from_env()
    
    # Get the note again to see the updated content
    try:
        note = client.notes_get_note(note_id=note_id)
        print(f"Retrieved note: {note['title']}")
        print("\nCURRENT NOTE CONTENT:")
        print("-" * 50)
        print(note['content'])
        print("-" * 50)
        
        return 0
    except Exception as e:
        print(f"Error retrieving note: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
