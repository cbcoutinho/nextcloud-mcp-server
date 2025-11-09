# Notes App

### Notes Tools

| Tool | Description |
|------|-------------|
| `nc_notes_create_note` | Create a new note with title, content, and category |
| `nc_notes_update_note` | Update an existing note by ID |
| `nc_notes_append_content` | Append content to an existing note with a clear separator |
| `nc_notes_delete_note` | Delete a note by ID |
| `nc_notes_search_notes` | Search notes by title or content (keyword search) |
| `nc_notes_semantic_search` | Search notes by meaning using vector embeddings (requires vector sync) |
| `nc_notes_semantic_search_answer` | Search notes semantically and generate a natural language answer via MCP sampling (requires vector sync and sampling-capable MCP client) |

### Note Attachments

This server supports adding and retrieving note attachments via WebDAV. Please note the following behavior regarding attachments:

* When a note is deleted, its attachments remain in the system. This matches the behavior of the official Nextcloud Notes app.
* Orphaned attachments (attachments whose parent notes have been deleted) may accumulate over time.
* WebDAV permissions must be properly configured for attachment operations to work correctly.
