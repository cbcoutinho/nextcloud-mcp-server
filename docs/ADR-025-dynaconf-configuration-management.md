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
| File-based config (TOML/YAML) | Yes (native) | No native TOML sections/env switching | `.env` only |
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

_config_root = Path(__file__).parent.parent

settings = Dynaconf(
    settings_files=["settings.toml", ".secrets.toml"],
    root_path=str(_config_root),
    environments=True,
    env_switcher="MCP_DEPLOYMENT_MODE",
    envvar_prefix=False,
    load_dotenv=False,
    ignore_unknown_envvars=True,
    validators=[...],  # See Section 4
)
```

Key choices:
- **`envvar_prefix=False`**: Existing env vars (`NEXTCLOUD_HOST`, `ENABLE_SEMANTIC_SEARCH`, etc.) work without any prefix. No renaming required.
- **`env_switcher="MCP_DEPLOYMENT_MODE"`**: Reuses the existing ADR-021 variable. Setting `MCP_DEPLOYMENT_MODE=single_user_basic` loads the `[single_user_basic]` TOML section on top of `[default]`. Note: dynaconf's `environments` feature is designed for lifecycle environments (dev/staging/prod), but custom environment names are a supported pattern — see `tests_functional/legacy/simple_ini_example/` in the dynaconf repo for a precedent using `environments=["ansible", "puppet"]`. **Legacy risk:** dynaconf docs flag `environments=True` as a legacy feature; if a future dynaconf major release removes it, we would need to migrate to the per-file approach (`settings.single_user_basic.toml`, etc.). This risk is acceptable given the alternative requires managing 5+ separate TOML files.
- **`ignore_unknown_envvars=True`**: Only env vars matching keys defined in `settings.toml` or defaults are loaded. System env vars (`HOME`, `PATH`, `LANG`) are ignored. **Important:** This means every env var the application reads must have a corresponding entry in `settings.toml`. See the `ignore_unknown_envvars` risk note under Consequences for mitigation.
- **`root_path=str(_config_root)`**: Anchors settings file lookup to the package's parent directory. Uses `str()` because dynaconf expects a string path. **Note on pip-installed packages:** When installed into a venv, `__file__` resolves to `site-packages/nextcloud_mcp_server/config.py` and `parent.parent` points inside site-packages — `settings.toml` will not be found there. This is intentional: pip-installed deployments are expected to use env vars (the primary configuration mechanism) or mount `settings.toml` into a location specified via `SETTINGS_FILE_FOR_DYNACONF`. The file-based config is a convenience for development and container deployments, not a requirement.
- **`load_dotenv=False`**: We don't auto-load `.env` files to avoid surprising behavior. Shell-level `.env` loading (e.g., `export $(grep -v '^#' .env | xargs)` as documented in CLAUDE.md) continues to work — env vars loaded into the shell before the process starts are picked up by dynaconf via its standard env var reading. Users who want automatic dotenv can use `direnv`.

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
simple_embedding_dimension = 384

# === Provider: Ollama ===
# ollama_base_url — Set via env var or .secrets.toml
ollama_generation_model = "@none"

# === Provider: Bedrock ===
aws_region = "@none"
bedrock_embedding_model = "@none"
bedrock_generation_model = "@none"
# aws_access_key_id — Set via env var or .secrets.toml
# aws_secret_access_key — Set via env var or .secrets.toml

# === Provider: Anthropic ===
# anthropic_api_key — Set via env var or .secrets.toml

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

# Provider API keys (any deployment mode)
# ollama_base_url = ""
# anthropic_api_key = ""
# openai_api_key = ""
# aws_access_key_id = ""
# aws_secret_access_key = ""
# qdrant_api_key = ""
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
    # Required unconditionally — needed in all deployment modes
    Validator("NEXTCLOUD_HOST", must_exist=True),

    # Deployment mode validation — catch typos at startup
    Validator("MCP_DEPLOYMENT_MODE", is_in=[
        "single_user_basic", "multi_user_basic", "login_flow",
        "keycloak", "oauth_single_audience",
    ], when=Validator("MCP_DEPLOYMENT_MODE", must_exist=True)),

    # Type and range validation
    Validator("METRICS_PORT", gte=1, lte=65535),
    Validator("VECTOR_SYNC_SCAN_INTERVAL", gte=1),
    Validator("VECTOR_SYNC_PROCESSOR_WORKERS", gte=1),
    Validator("DOCUMENT_CHUNK_SIZE", gte=128),
    Validator("DOCUMENT_CHUNK_OVERLAP", gte=0),

    # OTEL_TRACES_SAMPLER_ARG only validated for ratio-based samplers
    Validator(
        "OTEL_TRACES_SAMPLER_ARG", gte=0.0, lte=1.0,
        when=Validator("OTEL_TRACES_SAMPLER", condition=lambda v: "ratio" in str(v)),
    ),

    # Enum validation
    Validator("LOG_FORMAT", is_in=["text", "json"]),
    Validator("LOG_LEVEL", is_in=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    Validator("OTEL_TRACES_SAMPLER", is_in=["always_on", "always_off", "parentbased_always_on", "parentbased_always_off", "traceidratio", "parentbased_traceidratio"]),

    # Mutual exclusivity: QDRANT_URL and non-default QDRANT_LOCATION cannot both be set.
    # QDRANT_LOCATION defaults to ":memory:", so check for non-default values.
    # Note: Validator does not support `ne=` as a constructor kwarg — use `condition=` instead.
    Validator("QDRANT_URL", must_exist=False, when=Validator("QDRANT_LOCATION", condition=lambda v: v != ":memory:")),
]
```

#### 5. Backward Compatibility: Deprecation Handling

Deprecated env var names (`VECTOR_SYNC_ENABLED`, `ENABLE_OFFLINE_ACCESS`) are mapped to their current equivalents. In **Phases 1-3**, this logic lives in the `get_settings()` adapter function, which already performs this mapping today via `_get_semantic_search_enabled()` and `_get_background_operations_enabled()`. Dynaconf simply replaces the `os.getenv()` calls that feed these helpers.

In **Phase 4 (optional, future)**, these could be migrated to dynaconf `post_hooks` — constructor callbacks that run after all sources are loaded. Each hook receives a clone of the settings and returns a dict of values to merge:

```python
# Phase 4 target (not implemented in Phases 1-3)
def handle_deprecations(settings):
    """Map deprecated variable names to current names (ADR-021 compatibility)."""
    overrides = {}
    if settings.exists("VECTOR_SYNC_ENABLED") and not settings.exists("ENABLE_SEMANTIC_SEARCH"):
        overrides["ENABLE_SEMANTIC_SEARCH"] = settings.VECTOR_SYNC_ENABLED
        logger.warning("VECTOR_SYNC_ENABLED is deprecated. Use ENABLE_SEMANTIC_SEARCH instead.")
    if settings.exists("ENABLE_OFFLINE_ACCESS") and not settings.exists("ENABLE_BACKGROUND_OPERATIONS"):
        overrides["ENABLE_BACKGROUND_OPERATIONS"] = settings.ENABLE_OFFLINE_ACCESS
        logger.warning("ENABLE_OFFLINE_ACCESS is deprecated. Use ENABLE_BACKGROUND_OPERATIONS instead.")
    return overrides if overrides else None

# Usage: Dynaconf(post_hooks=[handle_deprecations, resolve_dependencies], ...)
```

**Note on `post_hooks` API:** This is a supported `Dynaconf()` constructor parameter (defined on `DynaconfConfig`, with `post_hooks` declared in `base.py` since dynaconf 3.2.x). It is verified to work in dynaconf's functional test suite (`tests_functional/legacy/ignore_unknown_envvars/app.py`). However, since the existing Python helpers already implement this logic correctly, migrating to `post_hooks` is deferred to Phase 4 to avoid changing execution context and ordering relative to `config_validators.py`.

#### 6. Smart Dependency Resolution

The auto-enablement of `ENABLE_BACKGROUND_OPERATIONS` when semantic search is active in multi-user modes (existing behavior from ADR-021) is preserved. In **Phases 1-3**, the existing `_is_multi_user_mode()` and `_get_background_operations_enabled()` helpers continue to work, reading from dynaconf instead of `os.getenv()`.

In **Phase 4**, this could migrate to a post-hook:

```python
# Phase 4 target (not implemented in Phases 1-3)
def resolve_dependencies(settings):
    """Auto-enable background operations for semantic search in multi-user modes."""
    is_multi_user = (
        settings.get("ENABLE_MULTI_USER_BASIC_AUTH", False)
        or settings.get("ENABLE_TOKEN_EXCHANGE", False)
        or (not settings.get("NEXTCLOUD_USERNAME") and not settings.get("NEXTCLOUD_PASSWORD"))
    )
    if settings.get("ENABLE_SEMANTIC_SEARCH", False) and is_multi_user:
        if not settings.get("ENABLE_BACKGROUND_OPERATIONS", False):
            logger.info("Auto-enabled background operations for semantic search in multi-user mode.")
            return {"ENABLE_BACKGROUND_OPERATIONS": True}
    return None
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
        enable_token_exchange=_dynaconf.get("ENABLE_TOKEN_EXCHANGE", False),
        # ... all fields populated from _dynaconf.get() instead of os.getenv()
    )
```

This is a zero-risk change: every consumer of `get_settings()` sees the same `Settings` type. **Every field on the `Settings` dataclass must have a corresponding `_dynaconf.get()` call** — omitting a field (e.g., `enable_token_exchange`) would silently regress functionality. The implementation should use a `_field_map` dict to make this exhaustive mapping auditable. The dataclass can be removed in a later phase once all consumers migrate to `_dynaconf` directly.

#### 8. Mode Detection Preserved

`config_validators.py` is unchanged in this phase. `detect_auth_mode()` and `validate_configuration()` continue to operate on the `Settings` dataclass. The business logic for mode detection, conditional requirements, and forbidden variables is too complex for declarative validators and benefits from remaining as explicit Python code.

#### 9. Document Processor Config Integration

`get_document_processor_config()` currently reads ~20 env vars independently. It will be migrated to read from the same dynaconf instance, with document processor settings nested under the `[default]` section alongside all other settings.

#### 10. Provider Registry (Phase 6)

`providers/registry.py:ProviderRegistry.create_provider()` reads ~15 env vars directly via `os.getenv()`. **Until Phase 6**, these calls remain unchanged — they are not broken by Phases 1-3 because `ignore_unknown_envvars` only affects dynaconf's own env var loading, not direct `os.getenv()` calls in other modules. However, all provider env vars must still be declared in `settings.toml` (see Section 2) so that dynaconf-based code can access them. In Phase 6, `ProviderRegistry` will be updated to accept a settings object or read from the dynaconf instance, consolidating all configuration into a single source.

#### 11. Test Isolation

Tests must not be affected by `settings.toml` or `.secrets.toml` being present in the repository. Dynaconf provides several test isolation patterns — we recommend the **fixture factory** approach as the primary strategy:

**Primary: Fresh instance per test (best isolation)**

```python
# conftest.py
import pytest
from dynaconf import Dynaconf

@pytest.fixture
def test_settings(tmp_path):
    """Create a fresh Dynaconf instance with no file-based config."""
    empty_toml = tmp_path / "settings.toml"
    empty_toml.write_text("[default]\n")
    return Dynaconf(
        settings_files=[str(empty_toml)],
        environments=True,
        env_switcher="MCP_DEPLOYMENT_MODE",
        envvar_prefix=False,
        FORCE_ENV_FOR_DYNACONF="testing",
    )
```

**Alternative: DynaconfDict for simple mocking**

```python
from dynaconf import DynaconfDict

def test_something():
    """Use DynaconfDict when only a few values are needed."""
    mock_settings = DynaconfDict({
        "NEXTCLOUD_HOST": "https://test.example.com",
        "ENABLE_SEMANTIC_SEARCH": False,
    })
    result = some_function(mock_settings)
```

**Alternative: Module-level reload for integration tests**

Dynaconf instances do support `reload()` (defined in `dynaconf/base.py`), which clears all loaded values and re-executes all loaders. This can be used for integration tests that need the full loading pipeline:

```python
@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Reset the module-level dynaconf instance for integration tests.

    Calls reload() + validate_all() in teardown to ensure the instance
    is both reset and in a valid state for subsequent tests.
    """
    monkeypatch.setenv("SETTINGS_FILE_FOR_DYNACONF", str(tmp_path / "empty.toml"))
    (tmp_path / "empty.toml").write_text("[default]\n")
    from nextcloud_mcp_server.config import _dynaconf
    _dynaconf.reload()
    yield
    _dynaconf.reload()
    # Re-validate after teardown to catch tests that leave invalid state
```

**Note on `_dynaconf` and `_reload_config`:** These are prefixed with `_` to signal internal use, but tests necessarily import them for isolation. This is an accepted trade-off. To prevent accidental production use, these names are intentionally excluded from `__all__` and carry docstrings noting they are test-accessible internals.

The fixture factory approach is preferred because it avoids global state mutation and is compatible with parallel test execution. Tests that need specific configuration values continue to use `monkeypatch.setenv()` as today, which overrides any file-based defaults (env vars have highest priority in dynaconf).

### Docker Compose Impact

**Zero breaking changes.** All existing `environment:` blocks in `docker-compose.yml` continue to work because `envvar_prefix=False` means env vars map directly to setting keys.

**Optional enhancement:** Users can mount settings files for cleaner configuration:

```yaml
mcp:
  volumes:
    # Note: settings.toml is checked into git, so it exists on the host.
    # If the host file is missing, Docker creates a directory instead — this
    # would cause a startup error, not silent misconfiguration.
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
- Add `.secrets.toml` and `settings.local.toml` to `.gitignore` (currently absent — existing `.gitignore` has `*.env` patterns but no dynaconf-specific entries)
- **Audit all `os.getenv()` calls** across the codebase (`config.py`, `providers/registry.py`, etc.) to ensure every env var has a corresponding `settings.toml` entry. This includes provider env vars (`AWS_REGION`, `BEDROCK_*`, `ANTHROPIC_API_KEY`, `OLLAMA_*`, `SIMPLE_EMBEDDING_DIMENSION`) which are critical because `ignore_unknown_envvars=True` silently drops unrecognized env vars.
- **Add CI lint check** (prerequisite for Phase 2): A script that extracts all `os.getenv()` keys and verifies each has a `settings.toml` entry. Phase 2 must not merge without this check passing in CI.
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
- **`ignore_unknown_envvars=True` silent failure mode** — Env vars not declared in `settings.toml` are silently ignored. If a developer adds a new env var but forgets to add a corresponding entry in `settings.toml`, the value will silently be `None` at runtime instead of producing an error. This inverts the current failure mode (where `os.getenv()` returning `None` at least fails visibly at the point of use). **Mitigation (mandatory before Phase 2):** Phase 1 must complete a full audit of all `os.getenv()` calls across the codebase — including `config.py`, `providers/registry.py`, and any other modules — and add corresponding entries to `settings.toml`. A CI lint check (e.g., a script that greps for `os.getenv()` keys and verifies each has a `settings.toml` entry) must be added as part of Phase 1, not deferred. Until this CI check is in place, `ignore_unknown_envvars=True` should not be enabled.
- **`ValidationError` replaces `ValueError`** — Dynaconf validators raise `dynaconf.validator.ValidationError` instead of `ValueError`. Any external code catching `ValueError` from `Settings.__post_init__` (e.g., for `document_chunk_overlap < 0`) will need to be updated. This is a breaking change introduced in Phase 3 when validators replace manual checks.
- **`environments=True` is a legacy dynaconf feature** — The dynaconf docs recommend against it for new projects. If a future dynaconf major release removes it, we would need to migrate to per-file configuration (`settings.single_user_basic.toml`, etc.) or pinned TOML section names. This risk is accepted because the alternative requires managing 5+ separate files with duplicated defaults.

### Neutral
- **`config_validators.py` unchanged** — Mode detection and conditional validation remain as Python business logic. Dynaconf validators handle structural checks only.
- **Docker Compose files unchanged** — Existing `environment:` blocks work as-is. File mounting is optional.
- **`environments=True` with custom deployment mode names** — When `MCP_DEPLOYMENT_MODE` is unset, only the `[default]` TOML section is loaded. This is the correct behavior: the existing auto-detection logic in `config_validators.py` still determines the deployment mode post-load based on which env vars are present. The TOML sections provide *defaults per mode*, not mode detection. Note: `env_switcher="MCP_DEPLOYMENT_MODE"` takes precedence over dynaconf's default `ENV_FOR_DYNACONF` variable. Contributors should not set `ENV_FOR_DYNACONF` directly, as it would shadow the `env_switcher` configuration and cause confusing behavior.

## Alternatives Considered

### 1. Pydantic Settings
Pydantic v2's `BaseSettings` provides type validation and env var loading. As of pydantic-settings 2.x, it supports TOML files via `PyprojectTomlConfigSettingsSource` and custom settings sources. However, it lacks native TOML section-based environment switching and automatic secrets file separation, which are the primary motivations for this change. While Pydantic v2 is already used in the project for response models (`nextcloud_mcp_server/models/`), Pydantic Settings would require significant custom code to replicate dynaconf's `[default]`/`[mode]` section merging and `.secrets.toml` auto-loading.

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
