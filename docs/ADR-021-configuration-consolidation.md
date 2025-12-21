# ADR-021: Configuration Consolidation and Simplification

**Status:** Accepted
**Date:** 2025-12-21
**Deciders:** Development Team
**Related:** ADR-020 (Deployment Modes), ADR-002 (Vector Sync), ADR-004 (Progressive Consent)

## Context

The configuration system has grown complex with overlapping concerns that make it difficult for users to switch between deployment modes and understand configuration dependencies.

### Problems Identified

1. **Confusing variable names don't reflect purpose**:
   - `ENABLE_OFFLINE_ACCESS` - Actually controls refresh token storage for background operations, not general "offline" capabilities
   - `VECTOR_SYNC_ENABLED` - Controls semantic search background indexing (implementation detail, not user-facing feature name)
   - Users struggle to understand what these variables actually control

2. **Redundant configuration requirements**:
   - Multi-user semantic search requires setting BOTH `ENABLE_OFFLINE_ACCESS=true` AND `VECTOR_SYNC_ENABLED=true`
   - The dependency is one-way (semantic search needs background ops, but background ops don't need semantic search)
   - Users must understand internal implementation details to configure a user-facing feature

3. **Implicit mode detection creates ambiguity**:
   - Five deployment modes detected via priority-based logic
   - Users can't easily predict which mode will activate
   - Configuration errors don't clearly indicate which mode triggered the requirement

4. **OIDC_CLIENT_ID vs NEXTCLOUD_OIDC_CLIENT_ID confusion**:
   - Investigation revealed these are NOT actually overlapping (`OIDC_CLIENT_ID` is test-only)
   - However, their similar names create confusion

### Current Configuration Complexity

**Example: Multi-user OAuth with semantic search**:
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_OFFLINE_ACCESS=true      # Why is this needed?
VECTOR_SYNC_ENABLED=true        # And this separately?
QDRANT_URL=http://qdrant:6333
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
```

Users must understand:
- Semantic search requires background token storage (ENABLE_OFFLINE_ACCESS)
- Background token storage requires encryption keys
- The relationship between ENABLE_OFFLINE_ACCESS and VECTOR_SYNC_ENABLED
- Which deployment mode these settings will activate

## Decision

We consolidate overlapping functionality and add explicit mode selection while maintaining 100% backward compatibility.

### 1. Automatic Dependency Resolution

**Make ENABLE_SEMANTIC_SEARCH the primary control** that automatically enables required dependencies:

**New behavior**:
```python
@property
def enable_background_operations(self) -> bool:
    """Background operations - auto-enabled by semantic search in multi-user modes."""
    # Check new names first
    explicit = os.getenv("ENABLE_BACKGROUND_OPERATIONS", "").lower() == "true"
    # Fall back to old name with deprecation warning
    legacy = os.getenv("ENABLE_OFFLINE_ACCESS", "").lower() == "true"
    # Auto-enable if semantic search needs it
    auto_enabled = self.enable_semantic_search and self.is_multi_user_mode()

    return explicit or legacy or auto_enabled

@property
def enable_semantic_search(self) -> bool:
    """Semantic search - renamed from VECTOR_SYNC_ENABLED."""
    new_value = os.getenv("ENABLE_SEMANTIC_SEARCH", "").lower() == "true"
    old_value = os.getenv("VECTOR_SYNC_ENABLED", "").lower() == "true"
    return new_value or old_value
```

**Result**: Users set `ENABLE_SEMANTIC_SEARCH=true` and the system automatically enables background token storage when needed.

### 2. Explicit Mode Selection (Optional)

Add `MCP_DEPLOYMENT_MODE` environment variable to remove detection ambiguity:

```bash
# Optional: Explicitly declare deployment mode
MCP_DEPLOYMENT_MODE=oauth_single_audience

# Valid values: single_user_basic, multi_user_basic,
#               oauth_single_audience, oauth_token_exchange, smithery
```

**Detection logic**:
1. If `MCP_DEPLOYMENT_MODE` is set → validate and use it
2. Otherwise → use priority-based auto-detection (existing behavior)
3. Validate explicit mode doesn't conflict with detected mode

### 3. Simplified User Experience

**Before**:
```bash
# Multi-user OAuth with semantic search
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_OFFLINE_ACCESS=true      # Confusing
VECTOR_SYNC_ENABLED=true        # Why both?
QDRANT_URL=http://qdrant:6333
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
```

**After**:
```bash
# Multi-user OAuth with semantic search
NEXTCLOUD_HOST=https://nextcloud.example.com
MCP_DEPLOYMENT_MODE=oauth_single_audience  # Explicit (optional)
ENABLE_SEMANTIC_SEARCH=true                # Auto-enables background ops
QDRANT_URL=http://qdrant:6333
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
```

**Benefits**:
- 2 fewer variables to understand/set
- Clear intent ("I want semantic search")
- Explicit mode declaration (optional)
- All existing configs continue working

### 4. Variable Naming Strategy

**Deprecated (but still functional)**:
- `ENABLE_OFFLINE_ACCESS` → Renamed to `ENABLE_BACKGROUND_OPERATIONS`
- `VECTOR_SYNC_ENABLED` → Renamed to `ENABLE_SEMANTIC_SEARCH`

**No change needed**:
- `VECTOR_SYNC_SCAN_INTERVAL` - Implementation tuning parameter (keep as-is)
- `VECTOR_SYNC_PROCESSOR_WORKERS` - Implementation tuning parameter (keep as-is)
- `VECTOR_SYNC_QUEUE_MAX_SIZE` - Implementation tuning parameter (keep as-is)

**Rationale**: Only rename user-facing feature flags, not internal tuning parameters.

### 5. Backward Compatibility

**Support both old and new names for minimum 2 major versions**:

```python
@property
def enable_semantic_search(self) -> bool:
    new_value = os.getenv("ENABLE_SEMANTIC_SEARCH", "").lower() == "true"
    old_value = os.getenv("VECTOR_SYNC_ENABLED", "").lower() == "true"

    if new_value and old_value:
        logger.warning(
            "Both ENABLE_SEMANTIC_SEARCH and VECTOR_SYNC_ENABLED are set. "
            "Using ENABLE_SEMANTIC_SEARCH. VECTOR_SYNC_ENABLED is deprecated."
        )

    if old_value and not new_value:
        logger.warning(
            "VECTOR_SYNC_ENABLED is deprecated. Please use ENABLE_SEMANTIC_SEARCH instead."
        )

    return new_value or old_value
```

**Deprecation timeline**:
- v0.6.0: Add new variables, deprecate old ones (both work with warnings)
- v1.0.0: Remove old variables (breaking change, well-announced)
- Minimum 2 major versions of support (12+ months)

## Consequences

### Positive

1. **Reduced cognitive load**: Users set `ENABLE_SEMANTIC_SEARCH=true` instead of understanding internal dependencies
2. **Clearer intent**: Variable names reflect user-facing features, not implementation details
3. **Explicit mode control**: `MCP_DEPLOYMENT_MODE` removes detection ambiguity
4. **Better onboarding**: New users see simpler configuration in env.sample
5. **Improved error messages**: Validation can suggest "set MCP_DEPLOYMENT_MODE=X" instead of relying on implicit detection
6. **No breaking changes**: All existing configurations continue working

### Negative

1. **Transition period complexity**: Both old and new names supported for 2+ versions
2. **Documentation burden**: All docs must be updated to show new approach
3. **Test coverage expansion**: Must test both old and new variable names in all modes
4. **Migration effort**: Existing deployments should eventually migrate (optional but recommended)

### Neutral

1. **Same functionality**: No new features, just better organization
2. **Same validation**: Underlying requirements unchanged (e.g., semantic search still needs Qdrant)
3. **Same performance**: No runtime performance impact

## Implementation

### Phase 1: Configuration Consolidation (v0.6.0)

**Files to modify**:
- `nextcloud_mcp_server/config.py` - Add property-based deprecation with auto-enablement
- `nextcloud_mcp_server/config_validators.py` - Simplify validation (semantic search no longer requires explicit background operations setting)
- `nextcloud_mcp_server/app.py` - Add informative logging for auto-enablement
- `tests/unit/test_config_validators.py` - Add auto-enablement tests
- `docs/configuration-migration-v2.md` - Create migration guide

**Key changes**:
1. `enable_background_operations` property auto-enables when `enable_semantic_search=true` in multi-user modes
2. `enable_semantic_search` property accepts both `ENABLE_SEMANTIC_SEARCH` and `VECTOR_SYNC_ENABLED`
3. Smart logging when auto-enablement occurs or deprecated variables used
4. Validation simplified to remove redundant requirements

### Phase 2: Explicit Mode Selection (v0.6.0)

**Files to modify**:
- `nextcloud_mcp_server/config.py` - Add `deployment_mode` field
- `nextcloud_mcp_server/config_validators.py` - Check explicit mode first, fall back to auto-detection
- `tests/unit/test_config_validators.py` - Test mode override and conflict detection
- `docs/configuration.md` - Document mode selection

**Key changes**:
1. Add `MCP_DEPLOYMENT_MODE` environment variable (optional)
2. Mode detection checks explicit mode first, then auto-detects
3. Validate explicit mode doesn't conflict with detected mode
4. Better error messages referencing explicit mode setting

### Phase 3: env.sample Reorganization (v0.6.0)

**Files to create/modify**:
- `env.sample` - Reorganize by deployment mode
- `env.sample.single-user` - Simplest config template
- `env.sample.oauth-multi-user` - Multi-user template showing consolidation
- `env.sample.oauth-advanced` - Token exchange mode template
- `README.md` - Update Quick Start to reference templates

**Key changes**:
1. Group related settings by deployment mode
2. Show simplified configuration (only essential variables)
3. Document automatic dependencies inline
4. Provide mode-specific quick-start templates

### Phase 4: Documentation Updates (v0.7.0)

**Files to modify**:
- `docs/configuration.md` - Lead with consolidated approach
- `docs/authentication.md` - Update mode guidance with `MCP_DEPLOYMENT_MODE`
- `docs/troubleshooting.md` - Add consolidation troubleshooting section
- `docs/configuration-migration-v2.md` - Expand with comprehensive examples
- `docs/ADR-020-deployment-modes-and-configuration-validation.md` - Update configuration matrix
- All other ADRs - Update variable references

**Key changes**:
1. Update all examples to use new variable names
2. Add before/after migration examples
3. Document automatic dependency resolution
4. Add mode selection decision tree diagram

## Validation Strategy

### Test Coverage Requirements

**Backward compatibility tests**:
- Old variable names still work (ENABLE_OFFLINE_ACCESS, VECTOR_SYNC_ENABLED)
- New variable names work (ENABLE_BACKGROUND_OPERATIONS, ENABLE_SEMANTIC_SEARCH)
- Setting both old and new triggers deprecation warning but works correctly
- All 41 existing config validation tests pass

**Auto-enablement tests**:
- `ENABLE_SEMANTIC_SEARCH=true` in OAuth mode → `enable_background_operations=true`
- `ENABLE_SEMANTIC_SEARCH=true` in single-user mode → `enable_background_operations=false` (not needed)
- `ENABLE_SEMANTIC_SEARCH=false` → `enable_background_operations=false` (unless explicitly set)

**Mode selection tests**:
- `MCP_DEPLOYMENT_MODE=oauth_single_audience` → mode correctly detected
- `MCP_DEPLOYMENT_MODE` conflicts with detected mode → validation error
- No `MCP_DEPLOYMENT_MODE` → auto-detection works as before

## Success Metrics

**Immediate** (v0.6.0 release):
- Zero breaking changes in existing deployments
- All 41 config validation tests pass
- New users report clearer configuration process

**Medium-term** (6 months after v0.6.0):
- 80% of new deployments use new variable names
- Mode selection errors decrease by 50%
- Support requests about configuration decrease

**Long-term** (12+ months):
- 90% of deployments migrated to new names
- Old variable names can be safely removed in v1.0.0
- Configuration-related issues in issue tracker decrease

## Alternatives Considered

### Alternative 1: Just Rename Variables

**Rejected**: User feedback: "There's no reason to just rename variables without consolidating functionality"

This would make names clearer but wouldn't reduce the number of variables users need to set. The real problem is requiring users to set both ENABLE_OFFLINE_ACCESS and VECTOR_SYNC_ENABLED when they just want semantic search.

### Alternative 2: Remove ENABLE_OFFLINE_ACCESS Entirely

**Rejected**: Advanced users need background operations without semantic search

Some deployments might want background token storage for future features (background Deck sync, background Calendar sync, etc.) without enabling semantic search. Keeping ENABLE_BACKGROUND_OPERATIONS (renamed) allows this.

### Alternative 3: Always Auto-Enable Background Operations

**Rejected**: Single-user mode doesn't need background token storage

Auto-enablement is only needed in multi-user modes. Single-user mode uses a shared client with BasicAuth, so background token storage is unnecessary. Always enabling it would waste resources and create confusing log messages.

### Alternative 4: Require All New Names Immediately

**Rejected**: Breaking change would affect all existing deployments

Forcing migration to new variable names in v0.6.0 would break every existing deployment. Supporting both old and new names with deprecation warnings provides a smooth migration path.

## References

- [ADR-020: Deployment Modes and Configuration Validation](ADR-020-deployment-modes-and-configuration-validation.md)
- [ADR-002: Vector Sync Authentication](ADR-002-vector-sync-authentication.md)
- [ADR-004: Progressive Consent](ADR-004-mcp-application-oauth.md)
- [Issue: Configuration complexity for multi-user semantic search](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/XXX)

## Migration Examples

### Example 1: Single-User BasicAuth with Semantic Search

**Before**:
```bash
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password
VECTOR_SYNC_ENABLED=true
QDRANT_LOCATION=:memory:
```

**After** (optional migration):
```bash
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password
ENABLE_SEMANTIC_SEARCH=true  # Renamed
QDRANT_LOCATION=:memory:
# Note: Background operations NOT auto-enabled (not needed in single-user mode)
```

### Example 2: Multi-User OAuth with Semantic Search

**Before**:
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_OFFLINE_ACCESS=true
VECTOR_SYNC_ENABLED=true
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
QDRANT_URL=http://qdrant:6333
```

**After** (simplified):
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
MCP_DEPLOYMENT_MODE=oauth_single_audience  # Explicit (optional)
ENABLE_SEMANTIC_SEARCH=true                # Auto-enables background operations
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
QDRANT_URL=http://qdrant:6333
# Note: ENABLE_OFFLINE_ACCESS no longer needed (auto-enabled)
```

### Example 3: Multi-User OAuth WITHOUT Semantic Search

**Before**:
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_OFFLINE_ACCESS=true  # For future background features
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
```

**After** (optional migration):
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
MCP_DEPLOYMENT_MODE=oauth_single_audience
ENABLE_BACKGROUND_OPERATIONS=true  # Renamed for clarity
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
```
