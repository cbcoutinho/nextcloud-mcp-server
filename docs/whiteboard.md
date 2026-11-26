# Whiteboard App

### Whiteboard Tools

| Tool | Description |
|------|-------------|
| `nc_whiteboard_list` | List the whiteboards in a folder and everything below it |
| `nc_whiteboard_get` | Read what is on a whiteboard, as a compact view |
| `nc_whiteboard_create` | Create an empty whiteboard |
| `nc_whiteboard_add_elements` | Add shapes, texts, arrows and frames |
| `nc_whiteboard_update_elements` | Change text, position, size or colours |
| `nc_whiteboard_delete_elements` | Delete elements |

The read tools need the `whiteboard.read` scope, the others `whiteboard.write`.
Neither needs `files.read` or `files.write`: a file is read or changed only if
Nextcloud reports it as a whiteboard, which is checked before its content is
downloaded. `EXCLUDED_TAGS` applies to all six, as it does to the file tools.

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

### Changing a board

Every change names the board's `etag`, from `nc_whiteboard_get` or from the
previous change, and returns the new one. A change to a board that was saved
since that etag is refused, and nothing is written.

- **An element is added complete**, as the app would save it, with an
  `index` after the last element. A shape takes its label as `text`, and an
  arrow takes the ids of the two elements it connects. New elements can be
  given ids, so that an arrow in the same call can refer to them.
- **Text is sized from its characters.** The app does not measure text when it
  loads a board, so the size is an estimate, on the wide side. A label is
  wrapped to its shape, and the shape grows to fit it.
- **Bindings stay in step.** A shape moves with its label, a frame with what is
  in it, and an arrow's ends follow the elements it connects. An element moved
  out of its frame leaves the frame, as when it is dragged in the app.
- **Nothing is removed from the file.** A deleted element is marked deleted, as
  the app does. An arrow that ended at it stays, unattached at that end.
- **Everything else is kept as it was**: other elements, unknown keys and the
  app's `customData`. A changed element gets a higher `version`, which is how
  the app merges two copies of a board.
- Locked elements, comments and tables are refused: only the app changes them.
- Each change reads the whole board and saves it again. A change that would make
  the board larger than 40 MB, or than `WEBDAV_WRITE_MAX_MB`, is refused.

### The open board

**A board that is open in a browser can undo a change.** The browser saves the
whole scene it holds, which does not include a change written meanwhile, and
neither side reports an error. The tools say so, so that the model can ask the
user to close the board first. Nextcloud keeps the earlier version of the file,
so a lost change can be found in its version history.

