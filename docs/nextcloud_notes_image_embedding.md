# Working with Images in Nextcloud Notes

This document explains how to properly work with images and attachments in Nextcloud Notes through the MCP server.

## Adding Image Attachments

Images and other files can be attached to notes using the WebDAV protocol. The Nextcloud MCP server handles this through the `add_note_attachment` method:

```python
# Example: Adding an image attachment to a note
client.add_note_attachment(
    note_id=123,  # The ID of the note
    filename="image.png",  # The filename for the attachment
    content=image_bytes,  # The binary content of the image
    mime_type="image/png"  # The MIME type
)
```

## Embedding Images in Notes

For images to display inline within notes, you must reference them correctly in the note content. There are two methods:

### 1. Markdown Syntax (Recommended)

```markdown
![Image Alt Text](.attachments.{note_id}/{filename})
```

For example:
```markdown
![My Screenshot](.attachments.123/screenshot.png)
```

### 2. HTML Image Tags

```html
<img src=".attachments.{note_id}/{filename}" alt="Image description" width="300" />
```

For example:
```html
<img src=".attachments.123/screenshot.png" alt="My Screenshot" width="300" />
```

## Storage Location

Image attachments are stored in a hidden directory structure:

```
/Notes/.attachments.{note_id}/{filename}
```

This path is accessible via WebDAV, allowing direct file operations.

## Orphaned Attachments Behavior

**Important:** When notes are deleted, their attachments remain in the system. This is the expected behavior of the official Nextcloud Notes app, not a bug in the MCP server implementation.

Consequences:
- Orphaned attachments accumulate over time
- No automatic cleanup of attachment directories
- References to attachments in deleted notes become broken links

## Examples

### Complete Example: Creating a Note with Embedded Image

```python
from nextcloud_mcp_server.client import NextcloudClient
import os

# Create client
client = NextcloudClient.from_env()

# 1. Create the note
note = client.notes_create_note(
    title="Note with Embedded Image",
    content="# Image Example\n\nThis note will have an embedded image.",
    category="Documentation"
)
note_id = note["id"]
note_etag = note["etag"]

# 2. Read image content
with open("example.png", "rb") as f:
    image_content = f.read()

# 3. Upload image as attachment
client.add_note_attachment(
    note_id=note_id,
    filename="example.png",
    content=image_content,
    mime_type="image/png"
)

# 4. Update note content to include image reference
updated_content = f"""# Image Example

This note has an embedded image below:

![Example Image](.attachments.{note_id}/example.png)
"""

# 5. Update the note with image reference
client.notes_update_note(
    note_id=note_id,
    etag=note_etag,
    content=updated_content
)
```

## Troubleshooting

If you encounter issues with attachments:

1. **401 Unauthorized errors**: Verify WebDAV permissions in Nextcloud
2. **Images not displaying**: Check the exact path format (`.attachments.{note_id}/{filename}`)
3. **Attachment access after note deletion**: This is expected - attachments persist after note deletion
