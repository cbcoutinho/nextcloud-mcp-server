# Activity App

| Tool | Description |
|------|-------------|
| `nc_activity_list_filters` | List the stream filters the instance offers (`all`, `self`, `by`, `files`, ...) |
| `nc_activity_list` | Read a page of the activity stream, with the cursor to continue from |

Both need the `activity.read` scope, and are hidden when the Activity app is not
installed. Supported on Nextcloud 33 and later.

## Finding what changed since the last session

1. To start, read the newest activity with `sort="desc", limit=1` and keep its
   `activity_id`.
2. Next session, call `nc_activity_list(sort="asc", since=<kept id>)`. Read on
   with each response's `next_since` while `has_more` is true.
3. Keep the last `next_since` for next time.

Things to know:

- **`has_more` is the only end-of-stream signal.** Nextcloud groups several
  changes into one activity, so a page shorter than `limit` does not mean the
  end.
- **Grouped activities.** `objects` lists every affected object (`{id: path}`
  for files); `object_name` names only the first one.
- **`cursor_reset: true`** means the server did not recognise `since` (an expired
  or invalid id) and restarted the stream from the beginning. Nextcloud answers
  that with a normal 200, so it is detected from the `X-Activity-First-Known`
  header, which the server sends only when it ignored the cursor. Changes since
  the old cursor may be missing: fall back to a full scan.
- An empty page (HTTP 304) is a normal "nothing new" and keeps `next_since`
  unchanged.

## Limitations

- Activity is not an audit log. Some changes (for example version labels) emit
  no activity, and activity expires (365 days by default).
- A file restored from the trash can get a new id.
- `EXCLUDED_TAGS` applies: an activity naming any excluded file is left out
  whole, never partly redacted, and counted in `excluded_count`. The exclusion
  is checked against the file's current tags and the path recorded in the
  activity; a file renamed out of an excluded folder since is matched by its old
  path.
