# WebDAV support

### WebDAV File System Tools

| Tool | Description |
|------|-------------|
| `nc_webdav_list_directory` | List files and directories in any NextCloud path |
| `nc_webdav_read_file` | Read file content (text files decoded, binary as base64) |
| `nc_webdav_write_file` | Create or update files in NextCloud |
| `nc_webdav_create_directory` | Create new directories |
| `nc_webdav_delete_resource` | Delete files or directories |
| `nc_webdav_move_resource` | Move or rename files and directories |
| `nc_webdav_copy_resource` | Copy files and directories |

### WebDAV File System Access

The server provides complete file system access to your NextCloud instance, enabling you to:

- Browse any directory structure
- Read and write files of any type
- Create and delete directories
- Manage your NextCloud files directly through LLM interactions

**Usage Examples:**

```python
# List files in root directory
await nc_webdav_list_directory("")

# Browse a specific folder
await nc_webdav_list_directory("Documents/Projects")

# Read a text file
content = await nc_webdav_read_file("Documents/readme.txt")

# Create a new directory
await nc_webdav_create_directory("NewProject/docs")

# Write content to a file
await nc_webdav_write_file("NewProject/docs/notes.md", "# My Notes\n\nContent here...")

# Delete a file or directory
await nc_webdav_delete_resource("old_file.txt")

# Move or rename a file
await nc_webdav_move_resource("document.txt", "new_name.txt")

# Move a file to another directory
await nc_webdav_move_resource("document.txt", "Archive/document.txt")

# Move a directory
await nc_webdav_move_resource("Projects/OldProject", "Projects/NewProject")

# Copy a file
await nc_webdav_copy_resource("document.txt", "document_copy.txt")

# Copy a file to another directory
await nc_webdav_copy_resource("document.txt", "Backup/document.txt")

# Copy a directory
await nc_webdav_copy_resource("Projects/ProjectA", "Projects/ProjectA_Backup")
```
