# ADR-020: Deployment Modes and Configuration Validation

**Status:** Accepted
**Date:** 2025-12-20
**Deciders:** Development Team
**Related:** ADR-002 (Vector Sync), ADR-004 (Progressive Consent), ADR-019 (Multi-user BasicAuth)

## Context

The MCP server supports multiple deployment scenarios with different authentication methods, storage backends, and feature sets. Over time, the configuration system evolved to support ~500+ possible combinations across deployment modes, authentication patterns, and feature toggles. This complexity made it difficult to:

1. Understand what configuration is required for a given deployment
2. Debug configuration errors (validation scattered across multiple files)
3. Provide helpful error messages when configuration is invalid
4. Maintain clear boundaries between deployment modes

**Problems Identified:**
- No single source of truth for "what config is required for mode X"
- Validation happening at 4+ different points (Settings.__post_init__, setup_oauth_config(), context helpers, starlette_lifespan)
- Startup sequence unclear (OAuth setup before FastMCP creation, sync initialization errors)
- Error messages generic ("X is required") without explaining which deployment mode triggered the requirement
- Multiple overlapping decision trees (deployment mode, auth mode, features)

## Decision

We formalize five distinct deployment modes with explicit configuration requirements and implement centralized configuration validation.

### Deployment Modes

#### 1. Single-User BasicAuth

**Use Case:** Personal Nextcloud instance, local development

**Required Configuration:**
```bash
NEXTCLOUD_HOST=http://localhost:8080
NEXTCLOUD_USERNAME=admin
NEXTCLOUD_PASSWORD=password  # Or app password
```

**Optional Configuration:**
```bash
# Vector sync (semantic search)
VECTOR_SYNC_ENABLED=true
QDRANT_LOCATION=/path/to/qdrant  # Or QDRANT_URL for remote

# Embeddings (optional - Simple provider used as fallback)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=nomic-embed-text

# Document processing
DOCUMENT_CHUNK_SIZE=512
DOCUMENT_CHUNK_OVERLAP=50
```

**Characteristics:**
- Single shared NextcloudClient created at startup
- No OAuth infrastructure needed
- No multi-user support
- Vector sync runs as single-user background task
- Admin UI available at /app

---

#### 2. Multi-User BasicAuth Pass-Through

**Use Case:** Internal deployment where users provide their own credentials, no background sync needed

**Required Configuration:**
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
ENABLE_MULTI_USER_BASIC_AUTH=true
```

**Optional Configuration:**
```bash
# For background sync (requires app passwords from Astrolabe)
ENABLE_OFFLINE_ACCESS=true
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
NEXTCLOUD_OIDC_CLIENT_ID=<client-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<client-secret>
VECTOR_SYNC_ENABLED=true
# ... plus Qdrant and embedding config
```

**Conditional Requirements:**
- If `ENABLE_OFFLINE_ACCESS=true`: requires `NEXTCLOUD_OIDC_CLIENT_ID`, `NEXTCLOUD_OIDC_CLIENT_SECRET`, `TOKEN_ENCRYPTION_KEY`, `TOKEN_STORAGE_DB`
- If `VECTOR_SYNC_ENABLED=true`: requires `ENABLE_OFFLINE_ACCESS=true`

**Characteristics:**
- No OAuth for client authentication (uses BasicAuth in request headers)
- BasicAuthMiddleware extracts credentials from Authorization header
- Client created per-request from extracted credentials
- Optional: Background sync using app passwords (via Astrolabe API)
- Admin UI available at /app

---

#### 3. OAuth Single-Audience (Default)

**Use Case:** Multi-user deployment with OAuth authentication, tokens work for both MCP and Nextcloud

**Required Configuration:**
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
# No NEXTCLOUD_USERNAME/PASSWORD (triggers OAuth mode)
```

**Auto-Configured:**
- OIDC discovery URL: `{NEXTCLOUD_HOST}/.well-known/openid-configuration`
- Client credentials: Dynamic Client Registration (DCR) if available
- Token storage: SQLite at `~/.oauth/clients.db`

**Optional Configuration:**
```bash
# Static client credentials (instead of DCR)
NEXTCLOUD_OIDC_CLIENT_ID=<client-id>
NEXTCLOUD_OIDC_CLIENT_SECRET=<client-secret>

# Offline access for background sync
ENABLE_OFFLINE_ACCESS=true
TOKEN_ENCRYPTION_KEY=<key>
TOKEN_STORAGE_DB=/path/to/tokens.db
VECTOR_SYNC_ENABLED=true
# ... plus Qdrant and embedding config

# Scopes
NEXTCLOUD_OIDC_SCOPES="openid profile email notes:read notes:write ..."
```

**Conditional Requirements:**
- If `ENABLE_OFFLINE_ACCESS=true`: requires `TOKEN_ENCRYPTION_KEY`, `TOKEN_STORAGE_DB`
- If `VECTOR_SYNC_ENABLED=true`: requires `ENABLE_OFFLINE_ACCESS=true`

**Characteristics:**
- Tokens contain both `aud: ["mcp-server", "nextcloud"]`
- Pass token through to Nextcloud APIs (no exchange)
- Client created per-request from token in Authorization header
- Background sync uses refresh tokens (if offline_access enabled)
- Admin UI available at /app

---

#### 4. OAuth Token Exchange (RFC 8693)

**Use Case:** Multi-user deployment where MCP token is separate from Nextcloud token

**Required Configuration:**
```bash
NEXTCLOUD_HOST=http://nextcloud.example.com
ENABLE_TOKEN_EXCHANGE=true
# No NEXTCLOUD_USERNAME/PASSWORD (triggers OAuth mode)
```

**Optional Configuration:**
- Same as OAuth Single-Audience, plus:
```bash
TOKEN_EXCHANGE_CACHE_TTL=300  # Cache exchanged tokens
```

**Characteristics:**
- Tokens contain only `aud: "mcp-server"`
- MCP server exchanges token for Nextcloud token via RFC 8693
- Exchanged tokens cached per-user
- Client created per-request using exchanged token
- Background sync uses refresh tokens (if offline_access enabled)

---

#### 5. Smithery Stateless

**Use Case:** Multi-tenant SaaS deployment via Smithery platform

**Required Configuration:**
- None! Configuration comes from session URL params: `?nextcloud_url=...&username=...&app_password=...`

**Forbidden Configuration:**
- Must NOT set: `NEXTCLOUD_HOST`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`, `ENABLE_MULTI_USER_BASIC_AUTH`, `ENABLE_TOKEN_EXCHANGE`, `ENABLE_OFFLINE_ACCESS`, `VECTOR_SYNC_ENABLED`, `NEXTCLOUD_OIDC_CLIENT_ID`, `NEXTCLOUD_OIDC_CLIENT_SECRET`

**Characteristics:**
- No persistent storage (stateless)
- Client created per-request from session config
- No vector sync (disabled)
- No admin UI (no /app routes)
- No OAuth infrastructure

---

### Configuration Validation

**Implementation:** `nextcloud_mcp_server/config_validators.py`

**Key Functions:**
```python
def detect_auth_mode(settings: Settings) -> AuthMode:
    """Detect authentication mode from configuration.

    Priority (most specific to most general):
    1. Smithery (explicit flag)
    2. Token exchange (most specific OAuth mode)
    3. Multi-user BasicAuth
    4. Single-user BasicAuth
    5. OAuth single-audience (default OAuth mode)
    """

def validate_configuration(settings: Settings) -> tuple[AuthMode, list[str]]:
    """Validate configuration for detected mode.

    Returns:
        Tuple of (detected_mode, list_of_errors)
        Empty list means valid configuration.
    """
```

**Validation Rules:**
- **Required variables:** Must be set and non-empty
- **Forbidden variables:** Must NOT be set (or must be False for booleans)
- **Conditional requirements:** If feature X is enabled, requires variables Y and Z

**Error Messages:**
```
Configuration validation failed for {mode} mode:
  - [{mode}] Missing required configuration: NEXTCLOUD_HOST
  - [{mode}] ENABLE_OFFLINE_ACCESS must be enabled when VECTOR_SYNC_ENABLED is true

Mode: {mode}
Description: {mode_description}

Required configuration:
  - VAR1
  - VAR2

Optional configuration:
  - VAR3
  - VAR4

Conditional requirements:
  When FEATURE is enabled:
    - VAR5
    - VAR6
```

**Integration:**
- Validation runs at app startup in `get_app()` (app.py:1048-1062)
- All errors reported before any initialization begins
- Mode-specific error messages explain requirements
- Validation uses the same Settings object used throughout the app

### Configuration Matrix

| Variable | Single BasicAuth | Multi BasicAuth | OAuth Single | OAuth Exchange | Smithery |
|----------|------------------|-----------------|--------------|----------------|----------|
| **NEXTCLOUD_HOST** | Required | Required | Required | Required | Forbidden |
| **NEXTCLOUD_USERNAME** | Required | Forbidden | Forbidden | Forbidden | Forbidden |
| **NEXTCLOUD_PASSWORD** | Required | Forbidden | Forbidden | Forbidden | Forbidden |
| **ENABLE_MULTI_USER_BASIC_AUTH** | Forbidden | Required | Forbidden | Forbidden | Forbidden |
| **ENABLE_TOKEN_EXCHANGE** | Forbidden | Forbidden | Forbidden | Required | Forbidden |
| **ENABLE_OFFLINE_ACCESS** | Optional\* | Optional\* | Optional\* | Optional\* | Forbidden |
| **TOKEN_ENCRYPTION_KEY** | If offline | If offline | If offline | If offline | Forbidden |
| **TOKEN_STORAGE_DB** | If offline | If offline | If offline | If offline | Forbidden |
| **OIDC_CLIENT_ID** | Forbidden | If offline | Optional\*\* | Optional\*\* | Forbidden |
| **OIDC_CLIENT_SECRET** | Forbidden | If offline | Optional\*\* | Optional\*\* | Forbidden |
| **VECTOR_SYNC_ENABLED** | Optional | Optional | Optional | Optional | Forbidden |
| **QDRANT_URL/LOCATION** | If vector | If vector | If vector | If vector | Forbidden |
| **OLLAMA_BASE_URL/OPENAI_API_KEY** | Optional | Optional | Optional | Optional | Forbidden |

\* Only enables background sync for semantic search
\*\* Uses DCR if not provided

## Consequences

### Positive

1. **Clarity:** Single function to detect mode from config
2. **Validation:** All config validated upfront with helpful errors
3. **Debugging:** Clear logs showing "Running in X mode with config Y"
4. **Maintenance:** Mode-specific logic can be isolated
5. **Documentation:** Clear mapping of mode → required config
6. **Error Messages:** Context-aware ("X is required for Y mode")
7. **Testing:** Each mode testable in isolation

### Negative

1. **Migration:** Existing invalid configurations will now fail at startup
2. **Flexibility:** Less flexibility in configuration combinations
3. **Strictness:** Some previously-working combinations may be rejected

### Neutral

1. **Backward Compatibility:** Valid configurations continue to work
2. **Mode Detection:** Automatic based on config (no explicit mode selection)
3. **Default Mode:** OAuth single-audience when no credentials provided

## Implementation Notes

### Embedding Provider Validation

Originally, validation required either `OLLAMA_BASE_URL` or `OPENAI_API_KEY` when vector sync was enabled. This was too strict because the Simple provider is always available as a fallback (ADR-015). The validation was removed to allow vector sync without explicit provider configuration.

### Variable Scoping Issues

During implementation, several Python variable scoping issues were discovered in `app.py`:
- Local variable assignments in `starlette_lifespan()` shadowed outer scope variables
- Fixed by using unique variable names (e.g., `nextcloud_host_for_context`, `basic_auth_storage`)
- Removed redundant `settings = get_settings()` call (re-used outer scope)

### Docker Compose Configuration

The `mcp-oauth` service configuration was updated to remove `ENABLE_MULTI_USER_BASIC_AUTH=true` which conflicted with its intended OAuth mode. The service now runs in OAuth single-audience mode with vector sync using the Simple embedding provider as fallback.

## Testing

### Unit Tests

`tests/unit/test_config_validators.py` provides comprehensive coverage:
- Mode detection with priority ordering (7 tests)
- Single-user BasicAuth validation (8 tests)
- Multi-user BasicAuth validation (7 tests)
- OAuth single-audience validation (6 tests)
- OAuth token exchange validation (3 tests)
- Smithery validation (4 tests)
- Mode summary generation (3 tests)
- Edge cases (3 tests)

**Total: 41 tests, all passing**

### Integration Tests

Integration tests verify that:
- Each mode starts successfully with valid configuration
- Invalid configurations fail with clear error messages
- Existing deployments continue to work

## References

- [ADR-002: Vector Sync Authentication](ADR-002-vector-sync-authentication.md)
- [ADR-004: Progressive Consent](ADR-004-progressive-consent.md)
- [ADR-015: Unified Provider Architecture](ADR-015-unified-provider-architecture.md)
- [ADR-019: Multi-user BasicAuth Pass-Through](ADR-019-multi-user-basicauth-passthrough.md)
- Implementation: `nextcloud_mcp_server/config_validators.py`
- Tests: `tests/unit/test_config_validators.py`
