# WebDAV support

### WebDAV File System Tools

| Tool | Description |
|------|-------------|
| `nc_webdav_list_directory` | List files and directories in any NextCloud path |
| `nc_webdav_read_file` | Read file content (documents extracted to text/markdown, text decoded, other binary as base64) |
| `nc_webdav_view_image` | Show an image to a vision model as an image, scaled to a useful size |
| `nc_webdav_write_file` | Create or update files in NextCloud |
| `nc_webdav_edit_file` | Change part of a plain UTF-8 text file by exact text replacement, without resending the whole file |
| `nc_webdav_create_directory` | Create new directories |
| `nc_webdav_delete_resource` | Delete files or directories |
| `nc_webdav_move_resource` | Move or rename files and directories |
| `nc_webdav_copy_resource` | Copy files and directories |
| `nc_webdav_list_comments` | Read the comments on a file |
| `nc_webdav_create_comment` | Comment on a file, mentioning people to notify them |

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

# Read a document: extracted text by default, no base64
result = await nc_webdav_read_file("Documents/report.pdf")
result.content  # the document's text
result.parse_tier  # "fast" | "structured" | "ocr" -- what produced it
result.content_format  # "text" | "markdown" | "base64"
result.parse_notes  # non-empty => say what degraded; this is not the whole document

# Ask for structure (headings, tables) instead of a flat text layer
await nc_webdav_read_file("Documents/report.pdf", parse_document="markdown")

# Read part of a long PDF: only these pages are parsed and returned
result = await nc_webdav_read_file("Documents/report.pdf", page_start=21, page_end=40)
result.page_count  # total pages in the document -- read on while page_end < page_count
result.page_start, result.page_end  # the pages this content covers (21, 40)

# Or take the file itself, unparsed
await nc_webdav_read_file("Documents/report.pdf", parse_document="raw")

# Read part of a long text file, with line numbers
result = await nc_webdav_read_file(
    "Logs/import.log", line_start=200, line_end=260, line_numbers=True
)
# result.total_lines says how long the file is

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

### Viewing Images

`nc_webdav_view_image` returns an image as an MCP image block, which clients
such as Claude Desktop and Claude Code pass to the model as a picture. That is
the difference to `nc_webdav_read_file`: it hands an image back as base64 text
inside its JSON (or as OCR text), which a model cannot look at, and a phone
photo as base64 is large enough to fill a context window on its own.

```python
# A photo, scaled to at most 1568 px on its longer edge (the default)
await nc_webdav_view_image("Photos/2026/whiteboard.jpg")

# More pixels to read small print, or fewer to save tokens
await nc_webdav_view_image("Scans/invoice.png", max_size=2576)
await nc_webdav_view_image("Photos/2026/whiteboard.jpg", max_size=512)
```

- **Nextcloud renders, the server scales.** The image is fetched through
  Nextcloud's preview endpoint (`/index.php/core/preview.png`), which applies
  the EXIF orientation and handles every format the instance can preview, HEIC
  included where the server supports it. The MCP server then scales it to
  `max_size` (64 to 2576, the most any Claude model looks at) and re-encodes
  it: JPEG, or PNG when the image has transparency. At the default of
  1568 px a photo arrives at 200 to 400 KB. Keep `max_size` at 2000 or
  below in long conversations: once a request carries more than 20 images,
  the Claude API refuses any image larger than that.
- **Fallback to the original.** When Nextcloud has no preview for a file, the
  original (up to 40 MB) is downloaded and decoded locally (JPEG, PNG, GIF,
  WebP, BMP, TIFF). A JPEG may have up to 80 megapixels, because the decoder
  skips resolution that would be scaled away. Every other format is decoded
  in full and is refused above 24 megapixels. Nextcloud previews are at most
  4096x4096 (16.8 megapixels), so the preview path never reaches either limit.
- **Images only.** Files whose name says they are something else (text, PDF,
  Markdown) are refused before any request, since Nextcloud would happily
  render them as pictures. Files without an extension are tried.
- **Anywhere in the files tree.** Notes attachments, images on Collectives
  pages and files shared in Talk are files like any other and work by path.
- The structured part of the response says how large the image is, whether it
  came from the preview or the original, and how large the image it was made
  from was, so a caller can tell whether a higher `max_size` would show more.

### File Comments

Comments annotate a file in place — review requests, hand-offs, context that
does not belong in the file itself. They are the same comments the Nextcloud
web UI shows in a file's sidebar.

```python
# Ask a colleague to review; @-mentioning them sends the notification
await nc_webdav_create_comment(
    "Reports/q3.pdf", 'numbers updated, please review @"alice"'
)

# Read the thread, newest first
await nc_webdav_list_comments("Reports/q3.pdf")

# Page through a long thread
await nc_webdav_list_comments("Reports/q3.pdf", limit=20, offset=20)
```

Mention a user by their Nextcloud user ID: `@alice`, or `@"user id with spaces"`.
Nextcloud parses the mention out of the message when it stores the comment and
sends the notification itself.

A comment is capped at **1000 characters** (measured after trimming whitespace,
counting Unicode code points — Nextcloud's own rule). Longer content belongs in
the file, with a short pointer comment.

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

### Editing Text Files in Place

`nc_webdav_edit_file` changes part of a text file without the caller sending
the whole file back. With `nc_webdav_write_file`, a one-line change to a long
Markdown page means the model has to reproduce the entire page as output:
slow, expensive, and an easy way to lose text it was not asked to touch. The
edit tool takes only the passage and its replacement, in the `str_replace`
form models are trained on:

```python
await nc_webdav_edit_file(
    "Collectives/Team/Plan.md",
    edits=[
        {"old_string": "- [ ] Draft agenda", "new_string": "- [x] Draft agenda"},
        {"old_string": "Owner: TBD", "new_string": "Owner: Alice"},
    ],
)
```

- **Exact, unique matches.** `old_string` must match the file exactly,
  whitespace and line breaks included, and occur exactly once. Otherwise the
  call fails and says why (not found, or found N times on which lines). Set
  `replace_all` on an edit to replace every occurrence deliberately.
- **All or nothing.** Several edits are applied in order and saved in one PUT,
  so they make one new file version. If any edit cannot be applied, nothing
  is written.
- **Safe against concurrent changes.** The server downloads the file and
  writes it back with `If-Match` on the etag it just read, so a change made
  elsewhere in between makes the edit fail instead of being overwritten. Pass
  `if_match` (an etag from an earlier read) to also refuse the edit if the file
  changed since you looked at it.
- **Line endings and BOM are kept.** Each edit is matched as written first. If
  that finds nothing and the file has the passage with Windows line endings
  (CRLF), edits written with `\n` are matched and written with CRLF. So a file
  that mixes LF and CRLF can be edited in both parts, and an edit that contains
  its own `\r` is never converted. In a file that uses CRLF throughout, lines
  an edit adds are written with CRLF too.
- **Checkable without re-reading.** The response carries the new `etag` and a
  unified `diff` of the change.
- **Plain text only.** The tool checks the file's content type before it
  decodes anything, because bytes that decode as UTF-8 are no proof of text: an
  all-ASCII PDF decodes fine, and an edit would shift the offsets in its xref
  table and corrupt it. Allowed are any `text/*` type, `+json` and `+xml`
  types (including SVG), and common text application types such as
  `application/json`, `application/yaml`, `application/toml`,
  `application/javascript`, `application/x-php` and `application/x-sh`. A file
  Nextcloud cannot classify (`application/octet-stream`) is allowed only if it
  has no extension (`Makefile`, `Dockerfile`, `.env`) or a well-known text
  extension such as `.ts`, `.ini` or `.log`. Everything else is refused with an
  error that names the type: PDFs, Office and OpenDocument files, images other
  than SVG, audio, video and archives. Use `nc_webdav_write_file` to replace
  such a file as a whole.
- **Limits.** The file must also decode as UTF-8. Files over
  `WEBDAV_WRITE_MAX_MB` are refused, since they could not be written back.
  Nextcloud's WebDAV has no partial writes, so the server still rewrites the
  whole file. That transfer is cheap, the saving is in what the model has to
  send.

To find the passage, `nc_webdav_read_file` takes `line_start`/`line_end` for
text files (any UTF-8 file, including JSON or YAML) and returns `total_lines`.
With `line_numbers=True` each line carries its number and a tab, which has to
be stripped again before quoting the text in an edit. A response is capped at
5 MB. When a window does not fit, the response ends at the last whole line and
`parse_notes` says where to continue. A single line larger than the cap (a
minified JSON file, say) is returned cut off at a character boundary, with
`line_end` naming that line and `parse_notes` saying that the rest of it is
not available. Text inside the returned part can still be edited.

### Write Size Limit

`nc_webdav_write_file` builds its request from a single in-memory MCP tool
argument — there is no chunked/streaming upload for writes (unlike the
read/ingest path). A pre-flight size gate rejects content over
`WEBDAV_WRITE_MAX_MB` (default 50, `0` disables) with a clear error rather
than risking a timeout or out-of-memory failure on a very large PUT.

## Conditional move and copy

`nc_webdav_move_resource` and `nc_webdav_copy_resource` accept an optional
`if_destination_match`. With `overwrite=True` alone the destination is replaced
unconditionally; supplying the destination's ETag replaces it **only if it is
still that exact version**, so a file someone else changed in the meantime is not
clobbered.

```python
info = await nc_webdav_read_file(path="Docs/report.txt")
await nc_webdav_move_resource(
    source_path="Drafts/report.txt",
    destination_path="Docs/report.txt",
    overwrite=True,
    if_destination_match=info["etag"],
)
```

### Why not `If-Match`?

`If-Match` applies to the **request-URI**, which for MOVE/COPY is the *source*.
Conditioning the destination requires RFC 4918 §10.4's tagged-list `If:` form,
which names the resource explicitly:

```
If: </remote.php/dav/files/user/Docs/report.txt> (["etag"])
```

### Limitations

Both come from sabre/dav and are surfaced rather than hidden:

- **Files only.** The etag is checked with `$node instanceof IFile`, so a
  **directory** destination always fails the condition with 412.
- **A missing destination yields 404, not 412.** An `If:` condition naming a URI
  that does not exist raises `NotFound` inside sabre. "The destination must
  exist" is therefore not expressible this way — `overwrite=False` already covers
  "must not exist".

`if_destination_match="*"` and combining it with `overwrite=False` both raise
`ValueError` at the client boundary rather than being silently reinterpreted.
