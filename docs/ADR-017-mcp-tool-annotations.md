# ADR-017: Add MCP Tool Annotations for Enhanced Client UX

## Status

Proposed

## Context

The MCP Python SDK supports tool annotations that provide behavioral hints and improved UX to MCP clients. Currently, our 101 tools across 10 modules lack these annotations, resulting in:

- Snake_case function names displayed to users (e.g., "nc_notes_create_note" instead of "Create Note")
- No behavioral hints for clients about read-only, destructive, or idempotent operations
- Missing parameter descriptions for better auto-completion and inline help
- Clients cannot optimize caching, warn before destructive operations, or retry safely

### Available MCP Annotations

The MCP SDK provides three types of annotations:

#### 1. Tool Decorator Parameters
```python
@mcp.tool(
    title="Human-Readable Name",
    description="Tool description",  # Can also come from docstring
    annotations=ToolAnnotations(...),
    icons=[Icon(...)]  # Optional visual icons
)
```

#### 2. ToolAnnotations Behavioral Hints
```python
from mcp.types import ToolAnnotations

ToolAnnotations(
    title="Alternative Title",  # Decorator title takes precedence
    readOnlyHint=True,         # Tool doesn't modify data
    destructiveHint=True,       # Tool may delete/overwrite data
    idempotentHint=True,        # Repeated calls with same args are safe
    openWorldHint=True          # Interacts with external entities
)
```

#### 3. Parameter Descriptions
```python
from pydantic import Field

async def tool(
    param: str = Field(description="What this parameter does"),
    ctx: Context
):
```

### Idempotency Analysis

**Important**: Idempotency means calling with **the same inputs** produces the same result.

**NOT Idempotent** (different inputs each call):
- **Updates with etag**: `update_note(id=1, title="X", etag="abc")` → etag changes to "def"
  - Second call: `update_note(id=1, title="X", etag="abc")` → fails (etag mismatch)
  - Different input (stale etag) → different result (error)
- **Creates**: `create_note(title="X")` → creates note 1
  - Second call → creates note 2 (different result)
- **Append operations**: `append_content(id=1, text="X")` → adds X once
  - Second call → adds X again (different result)

**Idempotent**:
- **Deletes**: `delete_note(id=1)` → note deleted
  - Second call → 404 or success (same end state: note doesn't exist)
  - Note: May return different status code, but end state is identical
- **Full resource PUT without version control**: Would be idempotent (we don't have these)
- **Set operations**: `set_property(id=1, value="X")` → property = X
  - Second call → property still = X (same result)
  - Note: Nextcloud updates use etags, so not applicable

**Read-Only** (always idempotent, never destructive):
- All list, search, get operations

## Decision

Add annotations to all 101 tools in three phases:

### Phase 1: Titles (Quick Win)
Add human-readable titles to all tools:

```python
@mcp.tool(title="Create Note")
async def nc_notes_create_note(...):
```

**Effort**: 2-3 hours
**Impact**: Immediate UX improvement

### Phase 2: ToolAnnotations (Behavioral Hints)
Add annotations based on corrected categorization:

```python
# Read-only tools
@mcp.tool(
    title="Search Notes",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        openWorldHint=True  # Nextcloud is external to MCP server
    )
)

# Delete tools (idempotent: same end state)
@mcp.tool(
    title="Delete Note",
    annotations=ToolAnnotations(
        destructiveHint=True,
        idempotentHint=True,  # Deleting deleted item = same end state
        openWorldHint=True
    )
)

# Create tools (not idempotent: creates multiple items)
@mcp.tool(
    title="Create Note",
    annotations=ToolAnnotations(
        idempotentHint=False,
        openWorldHint=True
    )
)

# Update tools with etag (not idempotent: etag changes)
@mcp.tool(
    title="Update Note",
    annotations=ToolAnnotations(
        idempotentHint=False,  # Etag required = different inputs each time
        openWorldHint=True
    )
)

# Append operations (not idempotent: adds content each time)
@mcp.tool(
    title="Append to Note",
    annotations=ToolAnnotations(
        idempotentHint=False,
        openWorldHint=True
    )
)
```

**Effort**: 4-6 hours
**Impact**: Better client behavior (caching, warnings, retry logic)

### Phase 3: Parameter Descriptions
Add Field() descriptions to parameters:

```python
from pydantic import Field

@mcp.tool(title="Create Note", annotations=ToolAnnotations(idempotentHint=False))
async def nc_notes_create_note(
    title: str = Field(description="The title of the note"),
    content: str = Field(description="Markdown content of the note"),
    category: str = Field(description="Category or folder name for organizing"),
    ctx: Context
) -> CreateNoteResponse:
```

**Effort**: 6-8 hours
**Impact**: Better auto-completion and inline help

## Tool Categorization

### Read-Only Tools (~40 tools)
**Pattern**: List, search, get operations
**Annotations**: `readOnlyHint=True`, `openWorldHint=True`

Examples:
- `nc_notes_search_notes` → "Search Notes"
- `nc_webdav_list_directory` → "List Files and Directories"
- `nc_calendar_list_calendars` → "List Calendars"
- `nc_contacts_get_contact` → "Get Contact"
- `nc_semantic_search` → "Semantic Search"
- `check_logged_in` → "Check Server Login Status"

### Create Tools (~20 tools)
**Pattern**: Create new resources
**Annotations**: `idempotentHint=False`, `openWorldHint=True`

Examples:
- `nc_notes_create_note` → "Create Note"
- `nc_calendar_create_event` → "Create Calendar Event"
- `nc_contacts_create_contact` → "Create Contact"
- `deck_create_card` → "Create Kanban Card"
- `nc_tables_create_row` → "Create Table Row"

### Update Tools (~25 tools)
**Pattern**: Modify existing resources with etag
**Annotations**: `idempotentHint=False` (etag changes), `openWorldHint=True`

Examples:
- `nc_notes_update_note` → "Update Note"
- `nc_calendar_update_event` → "Update Calendar Event"
- `nc_contacts_update_contact` → "Update Contact"
- `deck_update_card` → "Update Kanban Card"

**Rationale**: Updates require etag, which changes after each update. Same parameters on second call will fail due to stale etag = NOT idempotent.

### Append/Accumulate Tools (~5 tools)
**Pattern**: Add content without replacing
**Annotations**: `idempotentHint=False`, `openWorldHint=True`

Examples:
- `nc_notes_append_content` → "Append to Note"

**Rationale**: Each call adds content, changing the result = NOT idempotent.

### Delete Tools (~10 tools)
**Pattern**: Remove resources
**Annotations**: `destructiveHint=True`, `idempotentHint=True`, `openWorldHint=True`

Examples:
- `nc_notes_delete_note` → "Delete Note"
- `nc_webdav_delete_resource` → "Delete File or Directory"
- `nc_calendar_delete_event` → "Delete Calendar Event"
- `nc_contacts_delete_contact` → "Delete Contact"

**Rationale**: Deleting already-deleted item results in same end state (item doesn't exist) = idempotent. Status code may differ, but outcome is identical.

### Special Cases

#### OAuth Provisioning Tools
```python
# Not read-only but requires user interaction
@mcp.tool(
    title="Grant Server Access to Nextcloud",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        idempotentHint=False,  # Creates new OAuth session each time
        openWorldHint=True
    )
)
async def provision_nextcloud_access(ctx: Context):
```

#### Semantic Search (Closed World)
```python
@mcp.tool(
    title="Semantic Search",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        openWorldHint=False  # Searches only indexed Nextcloud data
    )
)
async def nc_semantic_search(query: str, ctx: Context):
```

**Rationale**: Semantic search only queries pre-indexed Nextcloud content, not the "open world" like web search would.

## Tool Priority Matrix

### Critical Priority (~2 tools)
OAuth tools required for server functionality:
- `provision_nextcloud_access` → "Grant Server Access to Nextcloud"
- `check_logged_in` → "Check Server Login Status"

### High Priority (~50 tools)
Most commonly used modules:
- **Notes** (14 tools): Create, read, update, delete notes
- **WebDAV** (13 tools): File operations
- **Calendar** (15 tools): Events and todos
- **Semantic Search** (6 tools): AI-powered search
- **Contacts** (9 tools): Address book operations

### Medium Priority (~35 tools)
Secondary functionality:
- **Deck** (9 tools): Kanban boards
- **Tables** (7 tools): Structured data
- **Sharing** (5 tools): File sharing

### Low Priority (~14 tools)
Less frequently used:
- **Cookbook** (8 tools): Recipe management
- **News** (6 tools): RSS feeds

## Implementation Plan

### Week 1: Phase 1 - Titles
- Add human-readable titles to all 101 tools
- Update tool name mapping in documentation
- Manual test in MCP inspector

### Week 2: Phase 2 - ToolAnnotations (High Priority)
- Add annotations to Critical and High priority tools (~52 tools)
- Focus on Notes, WebDAV, Calendar, Semantic, OAuth
- Add unit tests validating annotation presence

### Week 3: Phase 2 - ToolAnnotations (Medium/Low Priority)
- Complete remaining tools (~49 tools)
- Deck, Tables, Contacts, Cookbook, News
- Update tool listings in README

### Week 4: Phase 3 - Parameter Descriptions
- Add Field() descriptions to Critical/High priority tools
- Start with OAuth, Notes, WebDAV modules
- Incremental completion over time

## Benefits

### For Users
- **Clearer UI**: "Create Note" vs "nc_notes_create_note"
- **Safety**: Warnings before destructive operations
- **Better help**: Parameter descriptions in auto-completion
- **Confidence**: Know which operations are safe to retry

### For MCP Clients
- **Caching**: Cache results from read-only tools
- **Safety prompts**: Warn before destructiveHint=true
- **Retry logic**: Safely retry idempotent operations
- **UI organization**: Group by behavior (reads vs writes vs deletes)
- **Performance**: Optimize based on hints

### For Developers
- **Self-documenting**: Behavior is explicit
- **Consistency**: Standard patterns across codebase
- **Testing**: Validate annotations match implementation
- **Maintenance**: Clear expectations for new tools

## Consequences

### Positive
- Immediate UX improvement with minimal effort
- Clients can make smarter decisions
- Self-documenting code
- Follows MCP best practices

### Negative
- Initial effort to add annotations (12-15 hours total)
- Must maintain annotations when adding new tools
- Risk of incorrect annotations misleading clients

### Neutral
- Annotations are hints, not guarantees
- Clients may ignore annotations
- Backward compatible (additive change)

### Mitigations
- **Incorrect annotations**: Add tests validating behavior matches hints
- **Maintenance burden**: Add to code review checklist and tool template
- **Documentation**: Update CLAUDE.md with annotation guidelines

## Examples

### Complete Annotated Tool (Delete)

```python
from mcp.types import ToolAnnotations
from pydantic import Field

@mcp.tool(
    title="Delete Note",
    annotations=ToolAnnotations(
        destructiveHint=True,   # Deletes data permanently
        idempotentHint=True,    # Same end state (note doesn't exist)
        openWorldHint=True      # Nextcloud is external
    )
)
@require_scopes("notes:write")
@instrument_tool
async def nc_notes_delete_note(
    note_id: int = Field(description="The ID of the note to delete permanently"),
    ctx: Context
) -> DeleteNoteResponse:
    """Delete a note permanently (requires notes:write scope)"""
    client = await get_client(ctx)
    # ... implementation ...
```

### Complete Annotated Tool (Update)

```python
@mcp.tool(
    title="Update Note",
    annotations=ToolAnnotations(
        idempotentHint=False,   # NOT idempotent: etag changes each update
        openWorldHint=True
    )
)
@require_scopes("notes:write")
@instrument_tool
async def nc_notes_update_note(
    note_id: int = Field(description="The ID of the note to update"),
    title: str | None = Field(
        default=None,
        description="New title (omit to keep current)"
    ),
    content: str | None = Field(
        default=None,
        description="New markdown content (omit to keep current)"
    ),
    category: str | None = Field(
        default=None,
        description="New category/folder (omit to keep current)"
    ),
    etag: str = Field(
        description="ETag from get_note (prevents concurrent modification)"
    ),
    ctx: Context
) -> UpdateNoteResponse:
    """Update an existing note's title, content, or category.

    The etag parameter is required to prevent overwriting concurrent changes.
    Get the current ETag by first calling nc_notes_get_note.
    If the note has been modified since you retrieved it, the update will fail.
    """
    client = await get_client(ctx)
    # ... implementation ...
```

### Complete Annotated Tool (Read-Only)

```python
@mcp.tool(
    title="Search Notes",
    annotations=ToolAnnotations(
        readOnlyHint=True,    # Doesn't modify data
        openWorldHint=True    # Queries Nextcloud
    )
)
@require_scopes("notes:read")
@instrument_tool
async def nc_notes_search_notes(
    query: str = Field(description="Search term to match in note titles or content"),
    ctx: Context
) -> SearchNotesResponse:
    """Search notes by title or content, returning id, title, and category.

    This is a read-only operation that searches across all user notes.
    Use nc_notes_get_note to retrieve the full content of matching notes.
    """
    client = await get_client(ctx)
    # ... implementation ...
```

## Testing Strategy

### Unit Tests
Add tests validating annotation presence and correctness:

```python
def test_notes_tools_have_annotations():
    """Verify all notes tools have appropriate annotations."""
    tools = get_registered_tools(mcp)

    # Check create tool
    create_tool = tools["nc_notes_create_note"]
    assert create_tool.title == "Create Note"
    assert create_tool.annotations.idempotentHint is False

    # Check delete tool
    delete_tool = tools["nc_notes_delete_note"]
    assert delete_tool.title == "Delete Note"
    assert delete_tool.annotations.destructiveHint is True
    assert delete_tool.annotations.idempotentHint is True

    # Check read-only tool
    search_tool = tools["nc_notes_search_notes"]
    assert search_tool.title == "Search Notes"
    assert search_tool.annotations.readOnlyHint is True
```

### Integration Tests
- Verify existing tests pass with annotations
- Manual testing in MCP inspector/client

### Documentation Updates
- Update README tool listings with new titles
- Add annotation guidelines to CLAUDE.md
- Include examples in developer documentation

## Open Questions

1. **Icons**: Should we add visual icons for tools? (Requires design work)
2. **Semantic search openWorldHint**: False (internal data only) or True (Nextcloud is external)?
3. **Read-only with side effects**: Should tools that log analytics still be readOnlyHint=true?

## References

- MCP Python SDK: `/home/chris/Software/python-sdk/`
- ToolAnnotations spec: `src/mcp/types.py:1247`
- FastMCP decorator: `src/mcp/server/fastmcp/server.py:444`
- Examples: `examples/fastmcp/parameter_descriptions.py`, `examples/fastmcp/icons_demo.py`

## Decision Timeline

- **Proposed**: 2025-12-11
- **Reviewed**: TBD
- **Accepted**: TBD
- **Implemented**: TBD
