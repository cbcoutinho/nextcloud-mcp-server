# Deck App

### Deck Tools

| Tool | Description |
|------|-------------|
| `deck_get_boards` | List all Deck boards |
| `deck_get_board` | Get a board (toggle `include_acl` / `include_users` / `include_labels`) |
| `deck_get_board_overview` | **Compact whole-board snapshot** — board → stacks → summary card rows in one call |
| `deck_get_stacks` | List stacks in a board (cards as compact summaries by default) |
| `deck_get_stack` | Get a single stack (cards as compact summaries by default) |
| `deck_get_archived_stacks` | List archived stacks and their cards |
| `deck_get_cards` | List cards in a stack (compact summaries by default) |
| `deck_get_card` | Get a single card in full detail |
| `deck_get_labels` / `deck_get_label` | List / get board labels |
| `deck_get_card_comments` | List card comments (compact, newest-first by default) |
| `deck_create_card_comment` | Comment on a card; `overflow="split"` posts an over-length message as a numbered thread |
| `deck_update_card_comment` | Edit your own comment (cannot be split — see below) |
| `deck_delete_card_comment` | Delete your own comment |
| `deck_create_board` | Create a new Deck board with title and color |
| `deck_create_stack` | Create a new stack in a board |
| `deck_update_stack` | Update stack title and order |
| `deck_delete_stack` | Delete a stack and all its cards |
| `deck_create_card` | Create a new card in a stack with full options (title, description, due date, etc.) |
| `deck_update_card` | Update any aspect of a card (title, description, owner, order, etc.) |
| `deck_delete_card` | Delete a card |
| `deck_archive_card` | Archive a card |
| `deck_unarchive_card` | Unarchive a card |
| `deck_reorder_card` | Reorder/move a card within a single board (between stacks on that board) |
| `deck_move_card_to_board` | Move a card to a stack on a different board, remapping board-scoped labels |
| `deck_create_label` | Create a new label in a board |
| `deck_update_label` | Update label title and color |
| `deck_delete_label` | Delete a label |
| `deck_assign_label_to_card` | Assign a label to a card |
| `deck_remove_label_from_card` | Remove a label from a card |
| `deck_assign_user_to_card` | Assign a user to a card |
| `deck_unassign_user_from_card` | Remove a user assignment from a card |

### Deck Resources
| Resource | Description |
|----------|-------------|
| `nc://Deck/boards` | List all deck boards |
| `nc://Deck/boards/{board_id}` | Get details of a specific board |
| `nc://Deck/boards/{board_id}/stacks` | List all stacks in a board |
| `nc://Deck/boards/{board_id}/stacks/{stack_id}` | Get details of a specific stack |
| `nc://Deck/boards/{board_id}/stacks/{stack_id}/cards` | List all cards in a stack |
| `nc://Deck/boards/{board_id}/stacks/{stack_id}/cards/{card_id}` | Get details of a specific card |
| `nc://Deck/boards/{board_id}/labels` | List all labels in a board |
| `nc://Deck/boards/{board_id}/labels/{label_id}` | Get details of a specific label |



### Compact Retrieval (token efficiency)

On large boards the full card objects (description, nested labels, assigned
users, attachments, etags) make `deck_get_stacks` responses too large to be
practical. The read tools therefore return **compact card summaries by
default** and support filtering so you fetch only what you need.

**Shared knobs** on `deck_get_cards`, `deck_get_stacks`, `deck_get_stack`
(and `deck_get_archived_stacks`, minus `status`):

| Parameter | Default | Effect |
|-----------|---------|--------|
| `detail` | `summary` | `summary` returns compact rows (id, title, stackId, labels as titles, assignee UIDs, due/done, counts, a short `descriptionPreview`); `full` returns the complete card objects (the pre-0.92 shape). |
| `status` | `open` | Filter before serialization: `open`, `done`, `archived`, or `all`. The first three **partition** the board (no overlap) — a card that is both done and archived is reported only under `archived`. |
| `label` | – | Only cards carrying a label with this exact title. |
| `assigned_to` | – | Only cards assigned to this user UID. |
| `description_max_length` | – | In `detail="full"`, truncate each description. |
| `description_preview_length` | `140` | In `detail="summary"`, length of the preview. |

**`deck_get_board_overview(board_id, status="open", label=…, assigned_to=…)`**
is the token-efficient way to see a whole board: it returns the board title,
its label legend, and every stack with compact card rows in a single call —
prefer it over `deck_get_board` + `deck_get_stacks` for "show me the board"
requests. Use `deck_get_card` for the full body of a specific card.

**Comments** — `deck_get_card_comments` returns compact comments
(`id`, `actorId`, `message`, `creationDateTime`) by default. Use
`detail="full"` for the complete objects, `message_max_length` to truncate,
`order` (`newest`/`oldest`) to sort the page, and `limit`/`offset` to page.

#### Long comments

Nextcloud caps a comment at **1000 characters**
(`IComment::MAX_MESSAGE_LENGTH`). The limit is measured **after trimming
surrounding whitespace**, in **Unicode code points** — so an emoji counts as
one, not four — and the check is `> 1000`, meaning exactly 1000 is accepted.
Markdown and `@`-mentions are *not* expanded before the check: what you send
is what is counted.

`deck_create_card_comment` takes an `overflow` parameter deciding what happens
above that limit:

| `overflow` | Behaviour |
|---|---|
| `"error"` (default) | Nothing is posted. The error states the exact overage, that nothing was written, and how many comments a split would take. |
| `"split"` | The message is posted as a numbered thread. |

Splitting cuts at the most structural boundary that fits — markdown heading,
then code fence, paragraph, line, sentence, and finally word — so a part never
ends mid-word, and `@`-mentions (including `@"names with spaces"`) are never
severed. Each part is prefixed `(i/N)`, part 1 becomes the comment, and parts
2..N are posted as replies to part 1 so the card renders one thread rather than
N unrelated comments. If you pass `parent_id`, part 1 replies to it and the
rest still reply to part 1.

The response carries the whole thread:

```jsonc
{
  "comment":    { "id": 881, "message": "(1/3) ## Shipped state ..." },  // part 1
  "parts":      [ /* every part in order; parts[0] is part 1 */ ],
  "part_count": 3
}
```

For a single comment, `parts` is `null` and `part_count` is `1`, so existing
callers reading `comment` are unaffected.

Two limits worth knowing:

- **At most 10 parts.** Past that the call is rejected before anything is
  posted. Content that long belongs in a note (`nc_notes_create_note`) or a
  file (`nc_webdav_write_file`) attached with `deck_attach_note` /
  `deck_attach_file`, with a short pointer comment linking to it.
- **Splitting is not atomic.** Deck has no transactional multi-comment
  endpoint, so if a later part fails the earlier ones stay posted. No rollback
  is attempted — deleting them is itself a write that can fail, and a
  half-deleted thread is worse than a labelled partial one. The error names the
  comment ids that were created so you can post only the remainder rather than
  re-sending everything.

**`deck_update_card_comment` has no `overflow`**: an update replaces one comment
in place, so it cannot become several. Post a follow-up comment instead of
growing an existing one past the limit.

> **Upstream caveat.** Deck's `CommentService::create()` translates an
> over-length message into a clean HTTP 400, but its `update()` has no such
> catch — the exception escapes to Deck's exception middleware and comes back as
> a masked HTTP 500 whose body says only "Internal server error", with the real
> cause left in the server log. This server therefore validates length
> client-side before calling update, and maps a 500 from that endpoint to a
> message naming the length limit as the likely cause.

*Known limitation:* a fenced code block straddling a cut renders as two broken
fences. The fence boundary is high in the preference order so this is rare.

> **Breaking change:** list tools now default to `detail="summary"`
> and `status="open"`. The previous `include_archived_cards` parameter has
> been replaced by `status` (`status="all"` includes archived cards, matching
> `include_archived_cards=True`). Pass `detail="full"` to restore the old
> per-card shape.



### Deck Project Management

The server provides complete Nextcloud Deck integration, enabling you to manage projects, tasks, and workflows:

- Create and manage boards, stacks, and cards
- Organize tasks with labels and user assignments
- Archive/unarchive cards and reorder within or between stacks
- Full CRUD operations on all Deck entities
- Browse project structure through hierarchical resources

**Usage Examples:**

```python
# Create a new project board
await deck_create_board(title="Website Redesign", color="1976D2")

# Create workflow stacks
await deck_create_stack(board_id=1, title="To Do", order=1)
await deck_create_stack(board_id=1, title="In Progress", order=2)
await deck_create_stack(board_id=1, title="Done", order=3)

# Create task cards with details
await deck_create_card(
    board_id=1,
    stack_id=1,
    title="Design new homepage",
    description="Create mockups for the new homepage layout",
    type="plain",
    order=1,
    duedate="2025-08-15T17:00:00"
)

# Create and assign labels for organization
await deck_create_label(board_id=1, title="High Priority", color="F44336")
await deck_create_label(board_id=1, title="UI/UX", color="9C27B0")

# Assign labels and users to cards
await deck_assign_label_to_card(board_id=1, stack_id=1, card_id=1, label_id=1)
await deck_assign_user_to_card(board_id=1, stack_id=1, card_id=1, user_id="designer")

# Move cards through workflow
await deck_reorder_card(
    board_id=1,
    stack_id=1,        # From "To Do"
    card_id=1,
    order=1,
    target_stack_id=2  # To "In Progress"
)

# Update task progress
await deck_update_card(
    board_id=1,
    stack_id=2,
    card_id=1,
    description="Homepage mockups completed, starting development",
    order=1
)

# Complete tasks
await deck_reorder_card(
    board_id=1,
    stack_id=2,        # From "In Progress"  
    card_id=1,
    order=1,
    target_stack_id=3  # To "Done"
)

# Archive completed cards
await deck_archive_card(board_id=1, stack_id=3, card_id=1)
```
