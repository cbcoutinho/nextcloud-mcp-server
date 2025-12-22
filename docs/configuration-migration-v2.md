# Configuration Migration Guide v2

**Version:** v0.58.0
**Status:** Active
**Related ADR:** [ADR-021: Configuration Consolidation and Simplification](ADR-021-configuration-consolidation.md)

## Overview

This guide helps you migrate from the old configuration variables to the new consolidated approach introduced in v0.58.0.

**Key Changes:**
- `VECTOR_SYNC_ENABLED` → `ENABLE_SEMANTIC_SEARCH`
- `ENABLE_OFFLINE_ACCESS` → `ENABLE_BACKGROUND_OPERATIONS`
- New: `MCP_DEPLOYMENT_MODE` for explicit mode selection
- Automatic dependency resolution: semantic search auto-enables background operations

**Backward Compatibility:**
- Old variable names still work in v0.58.0+
- Deprecation warnings logged when old names used
- Old names will be removed in v1.0.0

---

## Quick Reference: Variable Name Changes

| Old Name | New Name | Status |
|----------|----------|--------|
| `VECTOR_SYNC_ENABLED` | `ENABLE_SEMANTIC_SEARCH` | Deprecated |
| `ENABLE_OFFLINE_ACCESS` | `ENABLE_BACKGROUND_OPERATIONS` | Deprecated |
| N/A (auto-detected) | `MCP_DEPLOYMENT_MODE` | New (optional) |

**Tuning parameters unchanged:**
- `VECTOR_SYNC_SCAN_INTERVAL` - Keep as-is
- `VECTOR_SYNC_PROCESSOR_WORKERS` - Keep as-is
- `VECTOR_SYNC_QUEUE_MAX_SIZE` - Keep as-is

---

## Migration Scenarios

### Scenario 1: Single-User BasicAuth with Semantic Search

**Before (v0.57.x):**
```bash
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password
VECTOR_SYNC_ENABLED=true
QDRANT_LOCATION=:memory:
OLLAMA_BASE_URL=http://ollama:11434
```

**After (v0.58.0+):**
```bash
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password

# Optional: Explicit mode declaration (recommended)
MCP_DEPLOYMENT_MODE=single_user_basic

# Updated variable name
ENABLE_SEMANTIC_SEARCH=true  # Previously VECTOR_SYNC_ENABLED

QDRANT_LOCATION=:memory:
OLLAMA_BASE_URL=http://ollama:11434
```

**What Changed:**
- ✅ Renamed `VECTOR_SYNC_ENABLED` to `ENABLE_SEMANTIC_SEARCH`
- ✅ Added optional `MCP_DEPLOYMENT_MODE` for clarity
- ✅ Background operations NOT auto-enabled (not needed in single-user mode)

**Migration Steps:**
1. Replace `VECTOR_SYNC_ENABLED=true` with `ENABLE_SEMANTIC_SEARCH=true`
2. Optionally add `MCP_DEPLOYMENT_MODE=single_user_basic`
3. Restart server
4. Verify deprecation warnings are gone

---

### Scenario 2: Multi-User OAuth with Semantic Search

**Before (v0.57.x):**
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=

# Both variables required - confusing!
ENABLE_OFFLINE_ACCESS=true
VECTOR_SYNC_ENABLED=true

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db
QDRANT_URL=http://qdrant:6333
OLLAMA_BASE_URL=http://ollama:11434
NEXTCLOUD_OIDC_CLIENT_ID=mcp-server
NEXTCLOUD_OIDC_CLIENT_SECRET=secret
```

**After (v0.58.0+ - Simplified):**
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=

# Optional: Explicit mode declaration
MCP_DEPLOYMENT_MODE=oauth_single_audience

# One variable does it all!
ENABLE_SEMANTIC_SEARCH=true  # Automatically enables background operations

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db
QDRANT_URL=http://qdrant:6333
OLLAMA_BASE_URL=http://ollama:11434
NEXTCLOUD_OIDC_CLIENT_ID=mcp-server
NEXTCLOUD_OIDC_CLIENT_SECRET=secret

# Note: ENABLE_OFFLINE_ACCESS no longer needed!
# Background operations are auto-enabled by ENABLE_SEMANTIC_SEARCH
```

**What Changed:**
- ✅ Removed need for explicit `ENABLE_OFFLINE_ACCESS`
- ✅ `ENABLE_SEMANTIC_SEARCH` automatically enables background operations in multi-user modes
- ✅ Renamed `VECTOR_SYNC_ENABLED` to `ENABLE_SEMANTIC_SEARCH`
- ✅ Added optional explicit mode declaration

**Migration Steps:**
1. Replace `VECTOR_SYNC_ENABLED=true` with `ENABLE_SEMANTIC_SEARCH=true`
2. Remove `ENABLE_OFFLINE_ACCESS=true` (auto-enabled)
3. Optionally add `MCP_DEPLOYMENT_MODE=oauth_single_audience`
4. Restart server
5. Check logs for confirmation: "Automatically enabled background operations for semantic search"

---

### Scenario 3: Multi-User OAuth WITHOUT Semantic Search

**Before (v0.57.x):**
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=

# Enable background operations for future features
ENABLE_OFFLINE_ACCESS=true

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db
NEXTCLOUD_OIDC_CLIENT_ID=mcp-server
NEXTCLOUD_OIDC_CLIENT_SECRET=secret
```

**After (v0.58.0+):**
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=

# Optional: Explicit mode declaration
MCP_DEPLOYMENT_MODE=oauth_single_audience

# Renamed for clarity
ENABLE_BACKGROUND_OPERATIONS=true  # Previously ENABLE_OFFLINE_ACCESS

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db
NEXTCLOUD_OIDC_CLIENT_ID=mcp-server
NEXTCLOUD_OIDC_CLIENT_SECRET=secret
```

**What Changed:**
- ✅ Renamed `ENABLE_OFFLINE_ACCESS` to `ENABLE_BACKGROUND_OPERATIONS`
- ✅ Added optional explicit mode declaration

**Migration Steps:**
1. Replace `ENABLE_OFFLINE_ACCESS=true` with `ENABLE_BACKGROUND_OPERATIONS=true`
2. Optionally add `MCP_DEPLOYMENT_MODE=oauth_single_audience`
3. Restart server

---

### Scenario 4: Multi-User BasicAuth with Semantic Search

**Before (v0.57.x):**
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_MULTI_USER_BASIC_AUTH=true

# Both required - redundant
ENABLE_OFFLINE_ACCESS=true
VECTOR_SYNC_ENABLED=true

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db
QDRANT_URL=http://qdrant:6333
OLLAMA_BASE_URL=http://ollama:11434
NEXTCLOUD_OIDC_CLIENT_ID=mcp-server
NEXTCLOUD_OIDC_CLIENT_SECRET=secret
```

**After (v0.58.0+ - Simplified):**
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_MULTI_USER_BASIC_AUTH=true

# Optional: Explicit mode declaration
MCP_DEPLOYMENT_MODE=multi_user_basic

# One variable handles both!
ENABLE_SEMANTIC_SEARCH=true  # Auto-enables background operations

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db
QDRANT_URL=http://qdrant:6333
OLLAMA_BASE_URL=http://ollama:11434
NEXTCLOUD_OIDC_CLIENT_ID=mcp-server
NEXTCLOUD_OIDC_CLIENT_SECRET=secret

# Note: ENABLE_OFFLINE_ACCESS no longer needed!
```

**What Changed:**
- ✅ Semantic search auto-enables background operations
- ✅ Removed need for explicit `ENABLE_OFFLINE_ACCESS`
- ✅ Clearer variable naming

**Migration Steps:**
1. Replace `VECTOR_SYNC_ENABLED=true` with `ENABLE_SEMANTIC_SEARCH=true`
2. Remove `ENABLE_OFFLINE_ACCESS=true` (auto-enabled)
3. Optionally add `MCP_DEPLOYMENT_MODE=multi_user_basic`
4. Restart server

---

### Scenario 5: Token Exchange Mode with Semantic Search

**Before (v0.57.x):**
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_TOKEN_EXCHANGE=true

# Both required
ENABLE_OFFLINE_ACCESS=true
VECTOR_SYNC_ENABLED=true

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db
TOKEN_EXCHANGE_CACHE_TTL=300
QDRANT_URL=http://qdrant:6333
OLLAMA_BASE_URL=http://ollama:11434
```

**After (v0.58.0+ - Simplified):**
```bash
NEXTCLOUD_HOST=https://nextcloud.example.com
ENABLE_TOKEN_EXCHANGE=true

# Optional: Explicit mode declaration
MCP_DEPLOYMENT_MODE=oauth_token_exchange

# One variable!
ENABLE_SEMANTIC_SEARCH=true  # Auto-enables background operations

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db
TOKEN_EXCHANGE_CACHE_TTL=300
QDRANT_URL=http://qdrant:6333
OLLAMA_BASE_URL=http://ollama:11434
```

**What Changed:**
- ✅ Semantic search auto-enables background operations
- ✅ Explicit mode declaration available

**Migration Steps:**
1. Replace `VECTOR_SYNC_ENABLED=true` with `ENABLE_SEMANTIC_SEARCH=true`
2. Remove `ENABLE_OFFLINE_ACCESS=true` (auto-enabled)
3. Optionally add `MCP_DEPLOYMENT_MODE=oauth_token_exchange`
4. Restart server

---

## Understanding Automatic Dependency Resolution

### How It Works

In v0.58.0+, the server uses smart dependency resolution:

```python
# In multi-user modes (OAuth, Multi-User BasicAuth):
if ENABLE_SEMANTIC_SEARCH == true:
    background_operations = automatically enabled
    refresh_tokens = automatically requested
    token_storage = required (TOKEN_ENCRYPTION_KEY, TOKEN_STORAGE_DB)
    oauth_credentials = required (for app password retrieval)
```

**What this means:**
- ✅ Set `ENABLE_SEMANTIC_SEARCH=true`
- ✅ Provide required infrastructure (Qdrant, Ollama, encryption key)
- ✅ System automatically enables background operations
- ❌ No need to set `ENABLE_BACKGROUND_OPERATIONS` separately

### When Automatic Enablement Happens

| Deployment Mode | Semantic Search Enabled | Background Operations Auto-Enabled? |
|----------------|------------------------|-----------------------------------|
| Single-User BasicAuth | ✅ | ❌ No (not needed) |
| Multi-User BasicAuth | ✅ | ✅ Yes |
| OAuth Single-Audience | ✅ | ✅ Yes |
| OAuth Token Exchange | ✅ | ✅ Yes |
| Smithery Stateless | N/A (not supported) | N/A |

### When to Explicitly Set ENABLE_BACKGROUND_OPERATIONS

Only needed when you want background operations **without** semantic search:

```bash
# Example: OAuth mode with background operations but NO semantic search
NEXTCLOUD_HOST=https://nextcloud.example.com
MCP_DEPLOYMENT_MODE=oauth_single_audience

# Explicitly enable background operations for future features
ENABLE_BACKGROUND_OPERATIONS=true

TOKEN_ENCRYPTION_KEY=your-key-here
TOKEN_STORAGE_DB=/app/data/tokens.db

# Semantic search disabled
ENABLE_SEMANTIC_SEARCH=false
```

---

## Explicit Mode Selection

### Why Use MCP_DEPLOYMENT_MODE?

**Benefits:**
- ✅ Removes ambiguity about which mode is active
- ✅ Validation errors reference specific mode requirements
- ✅ Catches configuration mistakes early
- ✅ Self-documenting configuration

**Example:**
```bash
# Without explicit mode:
NEXTCLOUD_HOST=https://nextcloud.example.com
# Is this OAuth or Multi-User BasicAuth? Not immediately clear.

# With explicit mode:
MCP_DEPLOYMENT_MODE=oauth_single_audience
NEXTCLOUD_HOST=https://nextcloud.example.com
# Clear: This is OAuth mode
```

### Valid Mode Values

| Mode Value | Description |
|-----------|-------------|
| `single_user_basic` | Single-user with username/password |
| `multi_user_basic` | Multi-user with BasicAuth pass-through |
| `oauth_single_audience` | Multi-user OAuth (recommended) |
| `oauth_token_exchange` | Multi-user OAuth with token exchange |
| `smithery` | Smithery platform deployment |

### Mode Detection Priority

When `MCP_DEPLOYMENT_MODE` is set:
1. ✅ Explicit mode is used
2. ✅ Server validates configuration matches explicit mode
3. ❌ Auto-detection is skipped

When `MCP_DEPLOYMENT_MODE` is NOT set:
1. ✅ Auto-detection runs (existing behavior)
2. ✅ Priority: Smithery → Token Exchange → Multi-User BasicAuth → Single-User BasicAuth → OAuth Single-Audience

---

## Validation and Error Messages

### Old Validation (v0.57.x)

```
Error: [multi_user_basic] ENABLE_OFFLINE_ACCESS is required when VECTOR_SYNC_ENABLED is enabled
```

**Problem:** User must understand internal dependency relationship

### New Validation (v0.58.0+)

```
Error: [multi_user_basic] TOKEN_ENCRYPTION_KEY is required when ENABLE_SEMANTIC_SEARCH is enabled
```

**Benefit:** Clear what's needed, no mention of internal ENABLE_BACKGROUND_OPERATIONS flag

---

## Troubleshooting Migration

### Issue: Deprecation Warning After Migration

**Symptom:**
```
WARNING: VECTOR_SYNC_ENABLED is deprecated. Please use ENABLE_SEMANTIC_SEARCH instead.
```

**Solution:**
1. Check for `VECTOR_SYNC_ENABLED` in `.env` file
2. Replace with `ENABLE_SEMANTIC_SEARCH`
3. Search for any scripts/CI configs using old name
4. Restart server

### Issue: Both Old and New Names Set

**Symptom:**
```
WARNING: Both ENABLE_SEMANTIC_SEARCH and VECTOR_SYNC_ENABLED are set. Using ENABLE_SEMANTIC_SEARCH.
```

**Solution:**
1. Remove `VECTOR_SYNC_ENABLED` from `.env`
2. Keep `ENABLE_SEMANTIC_SEARCH`
3. Restart server

### Issue: Missing Required Dependencies

**Symptom:**
```
Error: [oauth_single_audience] TOKEN_ENCRYPTION_KEY is required when ENABLE_SEMANTIC_SEARCH is enabled
```

**Solution:**
When semantic search is enabled in multi-user modes, you need:
- `TOKEN_ENCRYPTION_KEY` - Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `TOKEN_STORAGE_DB` - Path to SQLite database (e.g., `/app/data/tokens.db`)
- `NEXTCLOUD_OIDC_CLIENT_ID` and `NEXTCLOUD_OIDC_CLIENT_SECRET` - For app password retrieval

### Issue: Unexpected Mode Detected

**Symptom:**
Server activates `oauth_single_audience` mode when you expected `multi_user_basic`

**Solution:**
Add explicit mode declaration:
```bash
MCP_DEPLOYMENT_MODE=multi_user_basic
ENABLE_MULTI_USER_BASIC_AUTH=true
```

---

## Testing Your Migration

### Step 1: Verify Configuration

```bash
# Set new variable names in .env
cat .env | grep -E "(ENABLE_SEMANTIC_SEARCH|ENABLE_BACKGROUND_OPERATIONS|MCP_DEPLOYMENT_MODE)"
```

### Step 2: Check for Old Variable Names

```bash
# Should return nothing after migration
cat .env | grep -E "(VECTOR_SYNC_ENABLED|ENABLE_OFFLINE_ACCESS)"
```

### Step 3: Start Server and Check Logs

```bash
# Start server
docker-compose up mcp

# Look for:
# 1. No deprecation warnings
# 2. Correct mode detected
# 3. Auto-enablement messages (if using semantic search in multi-user mode)
```

**Expected Log Output (Multi-User OAuth + Semantic Search):**
```
INFO: Using explicit deployment mode: oauth_single_audience
INFO: Automatically enabled background operations for semantic search in multi-user mode.
INFO: Vector sync enabled. Starting background scanner...
```

### Step 4: Verify Functionality

Test that existing features still work:
- [ ] Semantic search returns results
- [ ] Background indexing runs
- [ ] OAuth flow completes successfully
- [ ] Refresh tokens are stored/retrieved

---

## Quick Start Templates

We provide mode-specific templates for new deployments:

| Template | Use Case |
|----------|----------|
| `env.sample.single-user` | Simplest setup |
| `env.sample.oauth-multi-user` | Recommended multi-user |
| `env.sample.oauth-advanced` | Token exchange mode |

**Usage:**
```bash
cp env.sample.oauth-multi-user .env
# Edit .env with your values
docker-compose up -d
```

---

## Timeline and Support

| Version | Status | Old Variable Support |
|---------|--------|---------------------|
| v0.57.x | Stable | Old names only |
| v0.58.0 | Current | Both old and new (with warnings) |
| v1.0.0 | Breaking | New names only |

**Recommendation:** Migrate before v1.0.0 (12+ months minimum)

---

## Getting Help

If you encounter issues during migration:

1. **Check the logs** - Look for deprecation warnings and error messages
2. **Review ADR-021** - See [docs/ADR-021-configuration-consolidation.md](ADR-021-configuration-consolidation.md)
3. **Use mode-specific templates** - See `env.sample.*` files
4. **File an issue** - Include your `.env` (redacted), logs, and mode

---

## Summary

**What You Need to Do:**
1. ✅ Rename `VECTOR_SYNC_ENABLED` → `ENABLE_SEMANTIC_SEARCH`
2. ✅ (Optional) Rename `ENABLE_OFFLINE_ACCESS` → `ENABLE_BACKGROUND_OPERATIONS`
3. ✅ (Recommended) Add `MCP_DEPLOYMENT_MODE` for clarity
4. ✅ Remove redundant settings (semantic search auto-enables background ops in multi-user modes)
5. ✅ Test your configuration

**What the Server Does Automatically:**
- ✅ Supports both old and new variable names
- ✅ Logs deprecation warnings for old names
- ✅ Auto-enables background operations when semantic search is enabled in multi-user modes
- ✅ Validates configuration and provides clear error messages

**Migration Timeline:**
- Now → v1.0.0: Both old and new names work
- v1.0.0+: Only new names supported

**Questions?** See [docs/configuration.md](configuration.md) or file an issue.
