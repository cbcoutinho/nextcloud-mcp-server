# ADR-009: Generic `semantic:read` OAuth Scope for Multi-App Vector Search

**Status**: Proposed
**Date**: 2025-01-11
**Depends On**: ADR-007 (Background Vector Sync), ADR-008 (MCP Sampling for Semantic Search)

## Context

ADR-007 established a background vector synchronization architecture that indexes content from multiple Nextcloud apps (notes, calendar events, deck cards, files, contacts) into a unified vector database. ADR-008 introduced semantic search tools (`nc_semantic_search`, `nc_semantic_search_answer`) that query this vector database and use MCP sampling to generate natural language answers.

The question is: **What OAuth scopes should protect semantic search operations?**

### Option 1: App-Specific Scopes

Require users to have scopes for each app they want to search:

```python
@mcp.tool()
@require_scopes("notes:read", "calendar:read", "deck:read", "files:read", "contacts:read")
async def nc_semantic_search(query: str, ctx: Context) -> SemanticSearchResponse:
    """Search across all indexed apps"""
```

**Advantages**:
- Granular control - users explicitly consent to searching each app
- Aligns with app-specific authorization model
- Clear security boundary - can only search apps you can access

**Disadvantages**:
- **Brittle user experience**: If a user grants only `notes:read` but the tool requires all 5 scopes, the tool becomes invisible/unusable
- **All-or-nothing enforcement**: Can't search notes alone - must grant all scopes or none
- **Poor progressive consent**: User can't start with notes search and later add calendar
- **Scope inflation**: Every new app adds another required scope
- **Mismatched semantics**: User thinks "I want to search my notes" but must grant calendar, deck, files, contacts just to make the tool appear

### Option 2: Single Generic Scope (Chosen)

Introduce a new semantic search-specific scope:

```python
@mcp.tool()
@require_scopes("semantic:read")
async def nc_semantic_search(query: str, ctx: Context) -> SemanticSearchResponse:
    """Search across all indexed apps"""
```

**Advantages**:
- **Simple authorization**: One scope grants semantic search capability
- **Progressive enablement**: User grants `semantic:read`, searches notes initially, then enables calendar indexing later
- **Logical grouping**: Semantic search is a cross-app feature, deserving its own scope
- **Future-proof**: New apps can be added to vector sync without changing OAuth scopes
- **Matches user mental model**: "I want semantic search" → grant `semantic:read` (not "I want semantic search" → grant 5 unrelated app scopes)

**Considerations**:
- User could search apps they can't directly access via app-specific tools
  - **Mitigation**: Dual-phase authorization (Phase 1: scope check passes with `semantic:read`, Phase 2: verify user can access each returned document via app-specific permissions)
- Less granular than app-specific scopes
  - **Counterpoint**: Semantic search is inherently cross-app - forcing per-app authorization defeats its purpose

### Option 3: Hybrid Approach (Rejected)

Support both: semantic search works with either `semantic:read` OR all app-specific scopes:

```python
@mcp.tool()
@require_scopes("semantic:read", alternative_scopes=["notes:read", "calendar:read", ...])
async def nc_semantic_search(query: str, ctx: Context) -> SemanticSearchResponse:
    """Search across all indexed apps"""
```

**Rejected Because**:
- Adds complexity to scope validation logic
- Unclear to users which scopes they should grant
- Alternative scopes still suffer from all-or-nothing problem
- No significant benefit over Option 2 with dual-phase authorization

## Decision

We will introduce two new OAuth scopes specifically for semantic search operations:

- **`semantic:read`**: Query vector database, perform semantic search, generate answers
- **`semantic:write`**: Enable/disable background vector synchronization, manage indexing settings

These scopes are **independent** of app-specific scopes (notes:read, calendar:read, etc.).

### Tool Scope Assignments

**Read Operations**:
```python
@mcp.tool()
@require_scopes("semantic:read")
async def nc_semantic_search(query: str, ctx: Context, limit: int = 10, score_threshold: float = 0.7) -> SemanticSearchResponse:
    """Semantic search across all indexed Nextcloud apps"""

@mcp.tool()
@require_scopes("semantic:read")
async def nc_semantic_search_answer(query: str, ctx: Context, limit: int = 5, max_answer_tokens: int = 500) -> SamplingSearchResponse:
    """Semantic search with LLM-generated answer via MCP sampling"""

@mcp.tool()
@require_scopes("semantic:read")
async def nc_get_vector_sync_status(ctx: Context) -> VectorSyncStatusResponse:
    """Get current vector synchronization status (indexed count, pending count, status)"""
```

**Write Operations**:
```python
@mcp.tool()
@require_scopes("semantic:write")
async def nc_enable_vector_sync(ctx: Context) -> VectorSyncResponse:
    """Enable background vector synchronization for this user"""

@mcp.tool()
@require_scopes("semantic:write")
async def nc_disable_vector_sync(ctx: Context) -> VectorSyncResponse:
    """Disable background vector synchronization"""
```

### Dual-Phase Authorization

To ensure users can only access documents they have permission to view, semantic search implements **dual-phase authorization**:

**Phase 1: Scope Check** (MCP Server)
- User must have `semantic:read` scope to call semantic search tools
- This grants permission to query the vector database

**Phase 2: Document Verification** (Per-Result Filtering)
- For each returned document, verify user has access via app-specific permissions
- Uses `DocumentVerifier` interface per app:
  - Notes: Call `/apps/notes/api/v1/notes/{id}` - if 404/403, exclude from results
  - Calendar: Call `/remote.php/dav/calendars/username/calendar/event.ics` - if 404/403, exclude
  - Deck: Call `/apps/deck/api/v1.0/boards/{board_id}/stacks/{stack_id}/cards/{card_id}` - if 404/403, exclude
  - Files: Call `/remote.php/dav/files/username/path` with PROPFIND - if 404/403, exclude
  - Contacts: Call `/remote.php/dav/addressbooks/username/addressbook/contact.vcf` - if 404/403, exclude

This two-phase approach ensures:
1. Semantic search is a **distinct capability** (like "global search") requiring explicit consent
2. Results are **filtered** to only include documents the user can access
3. No privilege escalation - users can't discover content they shouldn't see

**Implementation**: See ADR-007 Phase 3 (Document Verification) and `DocumentVerifier` interface.

### Scope Discovery

The new scopes will be:
- **Advertised** via PRM endpoint (`/.well-known/oauth-protected-resource/mcp`)
- **Dynamically discovered** from `@require_scopes` decorators on semantic search tools
- **Documented** in OAuth architecture (oauth-architecture.md)
- **Included** in default client registration scopes

## Consequences

### Benefits

**User Experience**:
- Simple authorization: one scope for semantic search capability
- Progressive enablement: grant `semantic:read`, enable indexing for apps later
- Natural mental model: "semantic search" is a distinct feature deserving its own scope

**Security**:
- Dual-phase authorization prevents privilege escalation
- Users explicitly consent to cross-app search capability
- Per-document verification ensures users only see accessible content

**Maintainability**:
- Adding new apps to vector sync doesn't require OAuth scope changes
- Clear separation between app access (notes:read) and search capability (semantic:read)
- Logical grouping of related operations (search, sync status, enable/disable)

**Future-Proof**:
- Can add new document types without breaking existing OAuth flows
- Supports future semantic features (recommendations, clustering) under same scope
- Aligns with potential future Nextcloud semantic capabilities

### Trade-offs

**Less Granular Than App-Specific Scopes**:
- User can't grant "semantic search notes only"
- Semantic search is all-or-nothing across enabled apps
- **Mitigation**: Dual-phase verification ensures users only see documents they can access

**New Scope to Learn**:
- Users must understand `semantic:read` is distinct from app scopes
- MCP clients must present scope clearly during consent
- **Mitigation**: Clear scope descriptions in OAuth consent UI and documentation

**Backend Complexity**:
- Requires dual-phase authorization implementation
- DocumentVerifier interface needed for each app
- **Benefit**: Enforces proper security regardless of scope model

### Migration Impact

**Breaking Change**: Existing deployments using notes-specific semantic search will break.

**Before (OLD - Breaking)**:
```python
@mcp.tool()
@require_scopes("notes:read")
async def nc_notes_semantic_search(query: str, ctx: Context) -> SemanticSearchResponse:
    """Semantic search notes"""
```

**After (NEW)**:
```python
@mcp.tool()
@require_scopes("semantic:read")
async def nc_semantic_search(query: str, ctx: Context) -> SemanticSearchResponse:
    """Semantic search across all apps"""
```

**Migration Path**:
1. Deploy server with new `semantic:read` scope
2. Users re-authenticate, granting `semantic:read` scope
3. Semantic search tools become visible/usable again
4. **No data loss**: Vector database and indexed documents remain unchanged

**Backward Compatibility**: None. This is an intentional breaking change to correct the scope model before broader adoption.

## Alternatives Considered

### Keep Notes-Specific Scopes

**Approach**: Continue using `notes:read` for semantic search, even when searching other apps.

**Rejected Because**:
- Semantically incorrect - searching calendar events is not "reading notes"
- Confuses users - why does searching calendar require notes:read?
- Doesn't scale - what scope for multi-app search?

### Create Per-App Semantic Scopes

**Approach**: Introduce `notes:semantic`, `calendar:semantic`, `deck:semantic`, etc.

**Rejected Because**:
- Scope proliferation - doubles the number of scopes
- Defeats purpose of unified vector search
- Users would need to grant 5+ scopes for cross-app search
- No clear benefit over dual-phase authorization with `semantic:read`

### Require All App Scopes (Already Rejected in Option 1)

**Approach**: Require `notes:read AND calendar:read AND deck:read AND files:read AND contacts:read`

**Rejected Because**: Unusable UX (see Option 1 disadvantages above)

## Related Decisions

**ADR-007**: Background Vector Sync provides the indexing architecture that semantic scopes protect. The DocumentVerifier interface from ADR-007 Phase 3 implements dual-phase authorization.

**ADR-008**: MCP Sampling for semantic search uses `semantic:read` to protect the sampling-enhanced search tool.

**ADR-004**: Progressive Consent architecture supports users granting `semantic:read` initially, then enabling per-app indexing via `semantic:write` (enable_vector_sync with app selection).

## Implementation Checklist

- [ ] Create ADR-009 document (this file)
- [ ] Update `oauth-architecture.md` to document `semantic:read` and `semantic:write` scopes ✅
- [ ] Update `README.md` to show Semantic Search as separate tool category ✅
- [ ] Update ADR-007 to reference `semantic:*` scopes instead of `sync:*` ✅
- [ ] Update ADR-008 to use `semantic:read` instead of `notes:read` ✅
- [ ] Implement DocumentVerifier interface for all apps (notes, calendar, deck, files, contacts)
- [ ] Update semantic search tools to use `@require_scopes("semantic:read")`
- [ ] Update vector sync tools to use `@require_scopes("semantic:write")`
- [ ] Add dual-phase authorization to semantic search implementation
- [ ] Test OAuth flow with `semantic:read` scope
- [ ] Update scope discovery in PRM endpoint
- [ ] Document migration path for existing deployments
