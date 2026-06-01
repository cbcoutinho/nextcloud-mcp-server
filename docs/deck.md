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
| `deck_create_board` | Create a new Deck board with title and color |
| `deck_create_stack` | Create a new stack in a board |
| `deck_update_stack` | Update stack title and order |
| `deck_delete_stack` | Delete a stack and all its cards |
| `deck_create_card` | Create a new card in a stack with full options (title, description, due date, etc.) |
| `deck_update_card` | Update any aspect of a card (title, description, owner, order, etc.) |
| `deck_delete_card` | Delete a card |
| `deck_archive_card` | Archive a card |
| `deck_unarchive_card` | Unarchive a card |
| `deck_reorder_card` | Move/reorder cards within or between stacks |
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
