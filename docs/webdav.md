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

### Safe Writes: Concurrent Edits and Locks

`nc_webdav_write_file` is **fail-closed** — it never silently overwrites an
existing file. Every write is a conditional PUT; the `if_match` argument
selects one of three modes:

| `if_match`            | Behaviour |
|-----------------------|-----------|
| omitted (`None`)      | **Create-only.** Fails if the path already exists. |
| an `etag`             | **Safe overwrite.** Fails if the file changed since that etag was read. |
| `"*"`                 | **Force-overwrite.** Overwrites unconditionally; fails only if the file does not exist. |

To change an existing file, read it first to obtain its `etag`
(`nc_webdav_read_file` returns one), then pass that `etag` back into the
write. If the file changed in the meantime (e.g. someone edited it directly
in the Nextcloud web UI), the write fails instead of clobbering their change.
`nc_webdav_list_directory` and the search/find tools also return an `etag`
per file, so you can obtain one without a full read.

```python
# Read, capture the etag, and write back safely
result = await nc_webdav_read_file("Documents/notes.md")
await nc_webdav_write_file(
    "Documents/notes.md", result["content"] + "\nMore.", if_match=result["etag"]
)
# Raises ToolError if the file changed since the read (etag mismatch, HTTP 412)
# or if it's locked by another client, e.g. open in the web editor (HTTP 423).

# Create a brand-new file (fails with ToolError if it already exists):
await nc_webdav_write_file("Documents/new.md", "# New")

# Deliberately replace an existing file wholesale, without reading it first:
await nc_webdav_write_file("Documents/notes.md", "# Regenerated", if_match="*")
```

> **Breaking change (0.x):** an `if_match`-less write over an *existing* file
> now fails with a `ToolError` rather than overwriting it (the previous
> last-write-wins behaviour). Pass the file's `etag`, or `if_match="*"` to
> force the overwrite.

### Write Size Limit

`nc_webdav_write_file` builds its request from a single in-memory MCP tool
argument — there is no chunked/streaming upload for writes (unlike the
read/ingest path). A pre-flight size gate rejects content over
`WEBDAV_WRITE_MAX_MB` (default 50, `0` disables) with a clear error rather
than risking a timeout or out-of-memory failure on a very large PUT.
