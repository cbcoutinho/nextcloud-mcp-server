# Whiteboard App

### Whiteboard Tools

| Tool | Description |
|------|-------------|
| `nc_whiteboard_list` | List the whiteboards in a folder and everything below it |
| `nc_whiteboard_get` | Read what is on a whiteboard, as a compact view |

Both need the `whiteboard.read` scope. They do not need `files.read`: a file is
read only if Nextcloud reports it as a whiteboard. `EXCLUDED_TAGS` applies to
both, as it does to the file tools.

### What `nc_whiteboard_get` returns

A board is an Excalidraw JSON file. Most of it is styling, element versions and
embedded images. The tool returns the visible elements only:

- **Every element** has its `id`, `type`, position and size, rounded to whole
  units, plus the `frame` and `groups` it belongs to and its `link`, if any.
- **A label** is the `text` of the shape or arrow that holds it, not a separate
  element.
- **An arrow** has the ids of the elements it connects, in `start` and `end`.
  An end that is not attached to anything has no id.
- **A table** is the text of its cells, and **a comment** is its thread.

Styling, image content and deleted elements are left out, and so is every field
an element does not have. A large board comes in pages: pass `next_offset` as
`offset` to read on. Every page reads the file again. If `etag` differs between
two pages, the board was saved in between: start again from offset 0.

### Limits

- **The result is the last saved state.** The browser saves a board some seconds
  after an edit, so recent changes on an open board may be missing.
- A board file over 40 MB is refused. A response holds about 100,000 characters
  of elements, and an element's text is cut off at 10,000 (`text_truncated`).
- A `.whiteboard` file that is not in the Excalidraw format is listed, but
  reading it is refused. Some stock templates, such as "SWOT analysis", are in
  the older Spacedeck format.
- The tools read the file, so they work while the Whiteboard app's collaboration
  backend is down.
