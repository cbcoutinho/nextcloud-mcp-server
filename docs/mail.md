# Mail App

Tools for the [Nextcloud Mail](https://github.com/nextcloud/mail) app: browsing
accounts and mailboxes, reading and searching messages, sending, and managing
message state (flags, tags, move, delete).

### Mail Tools

| Tool | Description |
|------|-------------|
| `nc_mail_list_accounts` | List the user's configured mail accounts |
| `nc_mail_list_mailboxes` | List the mailboxes (folders) of an account |
| `nc_mail_list_messages` | List message envelopes in a mailbox, with an optional filter query (see below) |
| `nc_mail_get_message` | Get one message with its full body and attachment metadata |
| `nc_mail_get_message_source` | Get a message's raw RFC 2822 source (all headers) |
| `nc_mail_get_attachment` | Download one attachment (base64; capped at 5 MB inline) |
| `nc_mail_send_message` | Send a message via the account's outbox |
| `nc_mail_set_flags` | Mark read/unread, star, mark answered or junk |
| `nc_mail_create_tag` | Create a tag, or look up an existing one by name |
| `nc_mail_set_tag` / `nc_mail_remove_tag` | Add / remove a tag on a message |
| `nc_mail_move_message` | Move a message to another mailbox |
| `nc_mail_delete_message` | Move a message to trash (or expunge it if already there) |

### Scopes

| Scope | Covers |
|-------|--------|
| `mail.read` | The listing, reading and attachment tools |
| `mail.write` | Flags, tags, move, delete |
| `mail.send` | `nc_mail_send_message` |

### Marking messages read

`nc_mail_set_flags` changes only the flags you pass; everything else is left
alone. It is the tool to reach for after an agent has processed a message, so it
does not keep looking unhandled:

```
nc_mail_set_flags(message_id=42, seen=True)              # mark read
nc_mail_set_flags(message_id=42, seen=False)             # back to unread
nc_mail_set_flags(message_id=42, flagged=True)           # star it
```

`seen`, `flagged` and `answered` are standard IMAP flags and always apply.
`junk` is a custom IMAP keyword — mail servers that do not advertise custom
keywords for the mailbox silently ignore it.

There is **no bulk flag endpoint** in the Mail app; call the tool per message.

### Filtering messages

`nc_mail_list_messages` accepts the Mail app's own filter grammar in
`search_filter`: space-separated `token:value` terms, ANDed together.

| Token | Values |
|-------|--------|
| `is:` / `not:` | `read`, `unread`, `starred`, `answered`, `important` |
| `from:` `to:` `cc:` `bcc:` | substring of the address or display name |
| `subject:` | substring |
| `body:` | substring — searched on the IMAP server, so slower |
| `tags:` | comma-separated tag **database ids** (not names) |
| `start:` / `end:` | date bounds |
| `flags:` | comma-separated `read`, `unread`, `starred`, `answered`, `important`, `attachments` |
| `match:anyof` | OR the from/to/cc/bcc/subject/body terms instead of ANDing |

```
nc_mail_list_messages(mailbox_id=10, search_filter="is:unread from:alice")
nc_mail_list_messages(mailbox_id=10, search_filter="subject:invoice start:2026-01-01")
```

### Tags

Mail tags are IMAP keywords, private to each user. A few notes that follow from
how the Mail app implements them:

- **Names normalise.** The IMAP label is derived as `$` + the lowercased name
  with spaces turned into underscores, so `"AI Index"`, `"ai index"` and
  `"ai_index"` are all the same tag.
- **`nc_mail_create_tag` is create-or-get.** Calling it for a name that already
  exists returns that tag instead of creating a duplicate. Since the Mail app
  exposes no tag-listing endpoint, this is also how you look a tag's id up —
  and the id is what the `tags:` filter needs.
- **`nc_mail_set_tag` creates the tag if needed**, so tagging works in one call.
- Tags attach to a message by its RFC `Message-ID`, so tagging propagates to
  copies of the same message in other mailboxes.

```
tag = nc_mail_create_tag(display_name="AI Index")        # -> tag.id = 7
nc_mail_set_tag(message_id=42, tag="AI Index")
nc_mail_list_messages(mailbox_id=10, search_filter="tags:7")
```

### Semantic search

Mail messages are indexed for semantic search as `doc_type="mail_message"` when
vector sync is enabled — see [semantic-search.md](semantic-search.md). Indexing
covers up to 100 messages per mailbox (the Mail API's per-request maximum;
paging beyond it is not implemented yet), and a message's sent timestamp is used
for change detection, since mail is immutable.

### Implementation notes

The Mail app publishes only a small OCS API; most of what these tools need lives
on its internal `/index.php/apps/mail/api/...` routes, which are CSRF-gated for
browser sessions. Nextcloud exempts any request carrying `OCS-APIRequest: true`
from that check, so an app password plus that header is sufficient — no
`requesttoken` round-trip. The GreenMail integration lane exercises this for
both the read and the write routes, so a regression shows up as a test failure
rather than as a silent assumption.

Because those routes are internal, the Mail app marks them
`OpenAPI::SCOPE_IGNORE` and may change them between releases. Breakage there
shows up as a failing integration test rather than a silent behaviour change.
