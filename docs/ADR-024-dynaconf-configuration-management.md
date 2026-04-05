# ADR-024: Dynaconf Configuration Management

**Status:** Proposed
**Date:** 2026-04-04
**Deciders:** Development Team
**Related:** ADR-020 (Deployment Modes), ADR-021 (Configuration Consolidation), ADR-022 (Login Flow v2)

## Context

The nextcloud-mcp-server configuration system has grown to ~80+ environment variables across five deployment modes. All configuration is loaded via manual `os.getenv()` calls in `config.py` (~60 calls in `get_settings()` alone) and `providers/registry.py`. This creates several problems:

### Problems Identified

1. **No file-based configuration option**: Every deployment requires setting environment variables. For complex deployments with 20+ variables (e.g., Keycloak + semantic search + observability), this is unwieldy and error-prone. There is no way to ship a "configuration profile" as a file.

2. **Configuration sprawl across multiple locations**: Environment variables are read in at least three places:
   - `config.py:get_settings()` — Main settings (~60 vars)
   - `config.py:get_document_processor_config()` — Document processing (~20 vars)
   - `providers/registry.py:ProviderRegistry.create_provider()` — Embedding providers (~15 vars)

3. **No configuration file for local development**: Developers must either maintain a `.env` file and `export $(grep -v '^#' .env | xargs)`, or rely solely on docker-compose environment blocks. A structured settings file with defaults per deployment mode would simplify onboarding.

4. **Manual type coercion is repetitive and error-prone**: The codebase is littered with patterns like:
   ```python
   os.getenv("SOME_BOOL", "false").lower() == "true"
   int(os.getenv("SOME_INT", "300"))
   float(os.getenv("SOME_FLOAT", "1.0"))
   ```
   Each is a potential `ValueError` if a user provides a non-numeric string for an integer field.

5. **No structured validation at load time**: While `config_validators.py` validates mode requirements after loading, there is no validation of individual field types, ranges, or mutual exclusivity at parse time. Invalid values (e.g., `METRICS_PORT=abc`) only fail when first used.

6. **Secrets mixed with configuration**: `TOKEN_ENCRYPTION_KEY`, `NEXTCLOUD_PASSWORD`, `OPENAI_API_KEY`, and other secrets are treated identically to non-sensitive configuration, with no separation mechanism.

### Current Configuration Surface

| Category | Approx. Vars | Example |
|----------|-------------|---------|
| Core Nextcloud | 6 | `NEXTCLOUD_HOST`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_VERIFY_SSL` |
| OAuth/OIDC | 12 | `OIDC_DISCOVERY_URL`, `NEXTCLOUD_OIDC_CLIENT_ID`, `JWKS_URI` |
| Mode Selection | 4 | `MCP_DEPLOYMENT_MODE`, `ENABLE_LOGIN_FLOW`, `ENABLE_TOKEN_EXCHANGE` |
| Token Storage | 3 | `TOKEN_ENCRYPTION_KEY`, `TOKEN_STORAGE_DB` |
| Semantic Search | 6 | `ENABLE_SEMANTIC_SEARCH`, `VECTOR_SYNC_SCAN_INTERVAL` |
| Qdrant | 4 | `QDRANT_URL`, `QDRANT_LOCATION`, `QDRANT_API_KEY` |
| Embedding Providers | 15 | `OLLAMA_BASE_URL`, `OPENAI_API_KEY`, `BEDROCK_*` |
| Document Processing | 18 | `ENABLE_UNSTRUCTURED`, `TESSERACT_CMD`, `PYMUPDF_*` |
| Observability | 10 | `OTEL_EXPORTER_OTLP_ENDPOINT`, `LOG_FORMAT`, `METRICS_PORT` |
| Webhooks/Internal | 4 | `WEBHOOK_INTERNAL_URL`, `NEXTCLOUD_MCP_SERVICE_NAME` |
| **Total** | **~82** | |

## Decision

Adopt [dynaconf](https://www.dynaconf.com/) as the configuration management layer, enabling TOML file-based configuration alongside existing environment variable support.

### Why Dynaconf

| Criterion | Dynaconf | Pydantic Settings | python-dotenv |
|-----------|----------|-------------------|---------------|
| File-based config (TOML/YAML) | Yes (native) | No (needs extra) | `.env` only |
| Environment sections/profiles | Yes (`[default]`, `[production]`) | No | No |
| Env var override (12-factor) | Yes (built-in, highest priority) | Yes | Yes |
| Type coercion | Automatic (TOML parser) | Via type hints | No |
| Validators | Declarative + conditional | Via Pydantic | No |
| Secrets file separation | Yes (`.secrets.toml`) | No built-in | Separate `.env` |
| Local overrides | Yes (`settings.local.toml` auto-loaded) | No | No |
| Zero-prefix env vars | Yes (`envvar_prefix=False`) | Custom | N/A |
| Dependency | Pure Python, well-maintained | Pydantic (already in project for models) | Minimal |

### Architecture

#### 1. Dynaconf Instance Configuration

```python
# nextcloud_mcp_server/config.py
from pathlib import Path
from dynaconf import Dynaconf, Validator

settings = Dynaconf(
    settings_files=["settings.toml", ".secrets.toml"],
    root_path=Path(__file__).parent.parent,
    environments=True,
    env_switcher="MCP_DEPLOYMENT_MODE",
    envvar_prefix=False,
    load_dotenv=False,
    ignore_unknown_envvars=True,
    post_hooks=[handle_deprecations, resolve_dependencies],
    validators=[...],  # See Section 4
)
```

Key choices:
- **`envvar_prefix=False`**: Existing env vars (`NEXTCLOUD_HOST`, `ENABLE_SEMANTIC_SEARCH`, etc.) work without any prefix. No renaming required.
- **`env_switcher="MCP_DEPLOYMENT_MODE"`**: Reuses the existing ADR-021 variable. Setting `MCP_DEPLOYMENT_MODE=single_user_basic` loads the `[single_user_basic]` TOML section on top of `[default]`. Note: dynaconf's `environments` feature is designed for lifecycle environments (dev/staging/prod), but custom environment names are a supported pattern — see `tests_functional/legacy/simple_ini_example/` in the dynaconf repo for a precedent using `environments=["ansible", "puppet"]`.
- **`ignore_unknown_envvars=True`**: Only env vars matching keys defined in `settings.toml` or defaults are loaded. System env vars (`HOME`, `PATH`, `LANG`) are ignored.
- **`root_path=Path(__file__).parent.parent`**: Anchors settings file lookup to the project root regardless of working directory. This ensures consistent behavior whether running via `uv run` from the repo root, inside a container with `WORKDIR /app`, or during test execution.
- **`post_hooks=[...]`**: Deprecation remapping and dependency resolution run after all sources are loaded (see Sections 5 and 6).
- **`load_dotenv=False`**: We don't auto-load `.env` files to avoid surprising behavior. Users who want dotenv can use `direnv` or shell-level loading.

#### 2. Settings File Structure

**`settings.toml`** — Shipped with the project, checked into git:

```toml
[default]
# === Nextcloud Connection ===
# nextcloud_host — Required, set via env var or .secrets.toml. No default.
nextcloud_verify_ssl = true
nextcloud_ca_bundle = "@none"

# === Deployment Mode ===
# Auto-detected if not set. Valid: single_user_basic, multi_user_basic,
# oauth_single_audience, login_flow, keycloak
# mcp_deployment_mode = ""

# === Authentication Toggles ===
enable_multi_user_basic_auth = false
enable_login_flow = false
enable_token_exchange = false

# === Token Storage ===
token_storage_db = "/tmp/tokens.db"

# === Semantic Search ===
enable_semantic_search = false
vector_sync_scan_interval = 300
vector_sync_processor_workers = 3
vector_sync_queue_max_size = 10000
vector_sync_user_poll_interval = 60

# === Qdrant ===
qdrant_location = ":memory:"
qdrant_collection = "nextcloud_content"

# === Embedding Providers ===
ollama_embedding_model = "nomic-embed-text"
ollama_verify_ssl = true
openai_embedding_model = "text-embedding-3-small"

# === Document Chunking ===
document_chunk_size = 2048
document_chunk_overlap = 200

# === Document Processing ===
enable_document_processing = false
document_processor = "unstructured"
enable_pymupdf = true
pymupdf_extract_images = true
enable_unstructured = false
unstructured_api_url = "http://unstructured:8000"
unstructured_timeout = 120
unstructured_strategy = "auto"
unstructured_languages = "eng,deu"
enable_tesseract = false
tesseract_lang = "eng"
enable_custom_processor = false
custom_processor_name = "custom"
custom_processor_types = "application/pdf"
custom_processor_timeout = 60

# === Observability ===
metrics_enabled = true
metrics_port = 9090
otel_service_name = "nextcloud-mcp-server"
otel_traces_sampler = "always_on"
otel_traces_sampler_arg = 1.0
otel_exporter_verify_ssl = false
log_format = "text"
log_level = "INFO"
log_include_trace_context = true

# === Webhooks ===
nextcloud_mcp_service_name = "mcp"
nextcloud_mcp_port = 8000

# ─────────────────────────────────────────────
# Deployment Mode Overrides
# ─────────────────────────────────────────────

[single_user_basic]
# Credentials provided via env vars or .secrets.toml
# nextcloud_username = ""  (in .secrets.toml)
# nextcloud_password = ""  (in .secrets.toml)

[multi_user_basic]
enable_multi_user_basic_auth = true
token_storage_db = "/app/data/tokens.db"

[login_flow]
enable_login_flow = true
token_storage_db = "/app/data/tokens.db"

[keycloak]
enable_token_exchange = true
token_storage_db = "/app/data/tokens.db"
token_exchange_cache_ttl = 300

[oauth_single_audience]
token_storage_db = "/app/data/tokens.db"
```

**`.secrets.toml.example`** — Template, checked into git (actual `.secrets.toml` is gitignored):

```toml
[default]
# token_encryption_key = ""

[single_user_basic]
# nextcloud_username = ""
# nextcloud_password = ""
# nextcloud_app_password = ""

[keycloak]
# nextcloud_oidc_client_id = ""
# nextcloud_oidc_client_secret = ""
# token_encryption_key = ""

[login_flow]
# token_encryption_key = ""
```

**`settings.local.toml`** — Personal overrides, gitignored, auto-loaded by dynaconf:

```toml
# Example developer overrides
[default]
log_level = "DEBUG"
ollama_base_url = "http://localhost:11434"
```

#### 3. Configuration Loading Priority

Dynaconf merges configuration in this order (last wins):

```
1. settings.toml [default] section          ← base defaults
2. settings.toml [<mode>] section           ← mode-specific overrides
3. .secrets.toml [default] section          ← base secrets
4. .secrets.toml [<mode>] section           ← mode-specific secrets
5. settings.local.toml (all sections)       ← developer overrides
6. Environment variables                    ← highest priority (12-factor)
```

This means:
- **File-based config is optional** — env vars alone still work (they override everything)
- **Mode-specific defaults reduce boilerplate** — `[login_flow]` sets `enable_login_flow=true` and `token_storage_db=/app/data/tokens.db` so deployers don't need to
- **Secrets are separated** — `.secrets.toml` holds `TOKEN_ENCRYPTION_KEY`, passwords, API keys
- **Local dev overrides don't pollute** — `settings.local.toml` is gitignored

#### 4. Dynaconf Validators

Replace repetitive `__post_init__` checks with declarative validators:

```python
validators = [
    # Required for all modes
    Validator("NEXTCLOUD_HOST", must_exist=True, when=Validator("MCP_DEPLOYMENT_MODE", ne="")),

    # Type and range validation
    Validator("METRICS_PORT", gte=1, lte=65535),
    Validator("VECTOR_SYNC_SCAN_INTERVAL", gte=1),
    Validator("VECTOR_SYNC_PROCESSOR_WORKERS", gte=1),
    Validator("DOCUMENT_CHUNK_SIZE", gte=128),
    Validator("DOCUMENT_CHUNK_OVERLAP", gte=0),
    Validator("OTEL_TRACES_SAMPLER_ARG", gte=0.0, lte=1.0),

    # Enum validation
    Validator("LOG_FORMAT", is_in=["text", "json"]),
    Validator("LOG_LEVEL", is_in=["DEBUG", "INFO", "WARNING", "ERROR"]),
    Validator("OTEL_TRACES_SAMPLER", is_in=["always_on", "always_off", "parentbased_always_on", "parentbased_always_off", "traceidratio", "parentbased_traceidratio"]),

    # Mutual exclusivity: QDRANT_URL and non-default QDRANT_LOCATION cannot both be set.
    # QDRANT_LOCATION defaults to ":memory:", so check for non-default values.
    Validator("QDRANT_URL", must_exist=False, when=Validator("QDRANT_LOCATION", ne=":memory:")),
]
```

#### 5. Backward Compatibility: Deprecation Hooks

Deprecated env var names (`VECTOR_SYNC_ENABLED`, `ENABLE_OFFLINE_ACCESS`) are handled via a post-hook that runs after all sources are loaded. Hooks are registered via `Dynaconf(post_hooks=[...])` (see Section 1):

```python
def handle_deprecations(settings):
    """Map deprecated variable names to current names (ADR-021 compatibility)."""
    # VECTOR_SYNC_ENABLED -> ENABLE_SEMANTIC_SEARCH
    if settings.exists("VECTOR_SYNC_ENABLED") and not settings.exists("ENABLE_SEMANTIC_SEARCH"):
        settings.set("ENABLE_SEMANTIC_SEARCH", settings.VECTOR_SYNC_ENABLED)
        logger.warning("VECTOR_SYNC_ENABLED is deprecated. Use ENABLE_SEMANTIC_SEARCH instead.")

    # ENABLE_OFFLINE_ACCESS -> ENABLE_BACKGROUND_OPERATIONS
    if settings.exists("ENABLE_OFFLINE_ACCESS") and not settings.exists("ENABLE_BACKGROUND_OPERATIONS"):
        settings.set("ENABLE_BACKGROUND_OPERATIONS", settings.ENABLE_OFFLINE_ACCESS)
        logger.warning("ENABLE_OFFLINE_ACCESS is deprecated. Use ENABLE_BACKGROUND_OPERATIONS instead.")
```

#### 6. Smart Dependency Resolution

The auto-enablement of `ENABLE_BACKGROUND_OPERATIONS` when semantic search is active in multi-user modes (existing behavior from ADR-021) is preserved as a post-hook:

```python
def resolve_dependencies(settings):
    """Auto-enable background operations for semantic search in multi-user modes."""
    is_multi_user = (
        settings.get("ENABLE_MULTI_USER_BASIC_AUTH", False)
        or settings.get("ENABLE_TOKEN_EXCHANGE", False)
        or (not settings.get("NEXTCLOUD_USERNAME") and not settings.get("NEXTCLOUD_PASSWORD"))
    )

    if settings.get("ENABLE_SEMANTIC_SEARCH", False) and is_multi_user:
        if not settings.get("ENABLE_BACKGROUND_OPERATIONS", False):
            settings.set("ENABLE_BACKGROUND_OPERATIONS", True)
            logger.info("Auto-enabled background operations for semantic search in multi-user mode.")
```

#### 7. Adapter Layer (Migration Bridge)

During migration, `get_settings()` continues to return the `Settings` dataclass, populated from dynaconf:

```python
from dynaconf import Dynaconf

_dynaconf = Dynaconf(...)  # As configured above

def get_settings() -> Settings:
    """Get application settings — backed by dynaconf."""
    return Settings(
        deployment_mode=_dynaconf.get("MCP_DEPLOYMENT_MODE"),
        nextcloud_host=_dynaconf.get("NEXTCLOUD_HOST"),
        nextcloud_username=_dynaconf.get("NEXTCLOUD_USERNAME"),
        # ... all fields populated from _dynaconf.get() instead of os.getenv()
    )
```

This is a zero-risk change: every consumer of `get_settings()` sees the same `Settings` type. The dataclass can be removed in a later phase once all consumers migrate to `_dynaconf` directly.

#### 8. Mode Detection Preserved

`config_validators.py` is unchanged in this phase. `detect_auth_mode()` and `validate_configuration()` continue to operate on the `Settings` dataclass. The business logic for mode detection, conditional requirements, and forbidden variables is too complex for declarative validators and benefits from remaining as explicit Python code.

#### 9. Document Processor Config Integration

`get_document_processor_config()` currently reads ~20 env vars independently. It will be migrated to read from the same dynaconf instance, with document processor settings nested under the `[default]` section alongside all other settings.

#### 10. Provider Registry

`providers/registry.py:ProviderRegistry.create_provider()` reads ~15 env vars directly. It will be updated to accept a settings object or read from the dynaconf instance, consolidating all configuration into a single source.

#### 11. Test Isolation

Tests must not be affected by `settings.toml` or `.secrets.toml` being present in the repository. The test configuration strategy:

```python
# conftest.py
import pytest

@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Ensure tests use a clean dynaconf instance with no file-based config."""
    monkeypatch.setenv("SETTINGS_FILE_FOR_DYNACONF", str(tmp_path / "empty.toml"))
    (tmp_path / "empty.toml").write_text("[default]\n")
    # Reset the dynaconf instance to pick up the override
    from nextcloud_mcp_server.config import _dynaconf
    _dynaconf.reload()
```

Tests that need specific configuration values continue to use `monkeypatch.setenv()` as today, which will override any file-based defaults (env vars have highest priority in dynaconf).

### Docker Compose Impact

**Zero breaking changes.** All existing `environment:` blocks in `docker-compose.yml` continue to work because `envvar_prefix=False` means env vars map directly to setting keys.

**Optional enhancement:** Users can mount settings files for cleaner configuration:

```yaml
mcp:
  volumes:
    - ./settings.toml:/app/settings.toml:ro
    - ./.secrets.toml:/app/.secrets.toml:ro
  environment:
    # Only override what differs from settings.toml
    - MCP_DEPLOYMENT_MODE=single_user_basic
    - LOG_LEVEL=DEBUG
```

## Migration Strategy

### Phase 1: Add Dynaconf Foundation
- Add `dynaconf` dependency to `pyproject.toml`
- Create `settings.toml` with `[default]` values matching current defaults
- Create `.secrets.toml.example` template
- Add `.secrets.toml` and `settings.local.toml` to `.gitignore`
- Initialize `Dynaconf` instance in `config.py`

### Phase 2: Wire Adapter
- Replace `os.getenv()` calls in `get_settings()` with `_dynaconf.get()` calls
- Replace `os.getenv()` calls in `get_document_processor_config()` similarly
- `Settings` dataclass and all consumers unchanged
- All tests pass without modification

### Phase 3: Add Validators
- Add dynaconf `Validator` instances for type checking, range validation, and enum constraints
- Remove corresponding manual checks from `Settings.__post_init__`

### Phase 4: Deprecation and Dependency Hooks (Optional, Future)
- Move `_get_semantic_search_enabled()`, `_get_background_operations_enabled()`, and `_is_multi_user_mode()` logic into dynaconf post-hooks
- Remove standalone helper functions
- **Risk note:** These functions contain nuanced multi-variable logic (e.g., the `ENABLE_SEMANTIC_SEARCH` + `VECTOR_SYNC_ENABLED` OR pattern, the username/password presence check for mode detection). Running them as dynaconf post-hooks changes their execution context and ordering guarantees relative to `config_validators.py`. This phase should only proceed after Phases 1-3 are stable and well-tested.

### Phase 5: Direct Dynaconf Access (Optional, Future)
- Gradually replace `get_settings().field` with `settings.FIELD` in consumers
- Remove `Settings` dataclass once all consumers migrated
- This is a larger refactor touching ~30 files and can be deferred

### Phase 6: Provider Registry Consolidation (Optional, Future)
- Update `ProviderRegistry.create_provider()` to read from dynaconf
- Eliminates the last pocket of direct `os.getenv()` calls

## Consequences

### Positive
- **File-based configuration** enables shipping deployment profiles, reducing per-deployment env var count from 15-25 to 1-3 overrides
- **Automatic type coercion** eliminates ~30 manual `int()`, `float()`, `.lower() == "true"` patterns and their potential `ValueError` exceptions
- **Declarative validation** catches invalid configuration at startup with clear error messages
- **Secret separation** via `.secrets.toml` provides a standard pattern for credential management
- **Local overrides** via `settings.local.toml` simplify developer workflows without polluting git
- **12-factor compliant** — env vars always win, files are optional
- **Zero breaking changes** in Phases 1-3. Phase 4 is optional and carries moderate risk due to complex multi-variable logic.

### Negative
- **New dependency** — `dynaconf` is a runtime dependency (~50KB, pure Python, well-maintained)
- **Two configuration systems during migration** — Phases 1-3 run dynaconf alongside the existing `Settings` dataclass
- **Learning curve** — Contributors must understand dynaconf's merge semantics and environment sections
- **`envvar_prefix=False` risk** — Without a prefix, any env var matching a setting key is loaded. Mitigated by `ignore_unknown_envvars=True` which restricts to pre-defined keys only

### Neutral
- **`config_validators.py` unchanged** — Mode detection and conditional validation remain as Python business logic. Dynaconf validators handle structural checks only.
- **Docker Compose files unchanged** — Existing `environment:` blocks work as-is. File mounting is optional.

## Alternatives Considered

### 1. Pydantic Settings
Pydantic v2's `BaseSettings` provides type validation and env var loading. However, it lacks native file-based configuration (TOML sections, environment switching, secrets files), which is the primary motivation for this change. While Pydantic v2 is already used in the project for response models (`nextcloud_mcp_server/models/`), Pydantic Settings still lacks native TOML sections, environment switching, and secrets file separation.

### 2. python-decouple
Supports `.env` and `.ini` files with type casting. Lacks environment sections, validators, secrets separation, and TOML support. Too limited for our needs.

### 3. Custom TOML Loader
Build a minimal TOML loader using `tomllib` (stdlib in Python 3.11+). This avoids a dependency but requires implementing validation, env var override, secrets separation, and environment switching from scratch — essentially rebuilding dynaconf.

### 4. Status Quo (Env Vars Only)
Continue with `os.getenv()`. Acceptable for small projects, but with 80+ variables across 5 deployment modes, the lack of file-based configuration, validation, and defaults per mode is a growing maintenance burden.

## References

- [Dynaconf Documentation](https://www.dynaconf.com/)
- [12-Factor App: Config](https://12factor.net/config)
- ADR-020: Deployment Modes and Configuration Validation
- ADR-021: Configuration Consolidation and Simplification
- ADR-022: Login Flow v2
