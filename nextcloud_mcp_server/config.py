import logging
import logging.config
import os
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dynaconf import Dynaconf, Validator

# Resolve root_path for dynaconf settings files.
# Editable installs: parent.parent is the project root (has settings.toml).
# Non-editable installs (Docker): settings.toml is mounted at WORKDIR (/app/).
_config_root = Path(__file__).parent.parent
if not (_config_root / "settings.toml").exists():
    _config_root = Path.cwd()

# Sentinel for "key not in dynaconf at all" vs "explicitly set to None".
_UNSET = object()

# Dynaconf instance — loads settings.toml + .secrets.toml + env vars.
# Env vars always win (12-factor). See ADR-024 for architecture.
_dynaconf = Dynaconf(
    settings_files=["settings.toml", ".secrets.toml"],
    environments=True,
    envvar_prefix=False,
    env_switcher="MCP_DEPLOYMENT_MODE",
    ignore_unknown_envvars=True,
    root_path=str(_config_root),
    load_dotenv=False,
    validators=[
        # Port ranges
        Validator("METRICS_PORT", gte=1, lte=65535),
        # Positive integers
        Validator("VECTOR_SYNC_SCAN_INTERVAL", gte=1),
        Validator("VECTOR_SYNC_PROCESSOR_WORKERS", gte=1),
        Validator("VECTOR_SYNC_QUEUE_MAX_SIZE", gte=1),
        Validator("VECTOR_SYNC_USER_POLL_INTERVAL", gte=1),
        Validator("DOCUMENT_CHUNK_SIZE", gte=1),
        # Non-negative
        Validator("DOCUMENT_CHUNK_OVERLAP", gte=0),
        # Enum constraints
        Validator("LOG_FORMAT", is_in=["text", "json"]),
        Validator(
            "LOG_LEVEL",
            is_in=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        ),
        Validator(
            "OTEL_TRACES_SAMPLER",
            is_in=[
                "always_on",
                "always_off",
                "traceidratio",
                "parentbased_always_on",
                "parentbased_always_off",
                "parentbased_traceidratio",
            ],
        ),
        # Float ranges
        Validator("OTEL_TRACES_SAMPLER_ARG", gte=0.0, lte=1.0),
    ],
)


def _reload_config():
    """Reload dynaconf settings from files and environment.

    Call this in tests after modifying os.environ to refresh the cache.
    Re-validates all validators since reload() only checks unchecked ones.
    """
    _dynaconf.reload()
    _dynaconf.validators.validate_all()


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "http",
        },
    },
    "formatters": {
        "http": {
            "format": "%(levelname)s [%(asctime)s] %(name)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "loggers": {
        "": {
            "handlers": ["default"],
            "level": "INFO",
        },
        "httpx": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,  # Prevent propagation to root logger
        },
        "httpcore": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,  # Prevent propagation to root logger
        },
        "uvicorn": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.access": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        },
        "uvicorn.error": {
            "handlers": ["default"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


def setup_logging():
    logging.config.dictConfig(LOGGING_CONFIG)


# Document Processing Configuration


def get_document_processor_config() -> dict[str, Any]:
    """Get document processor configuration from dynaconf.

    Returns:
        Dict with processor configs:
        {
            "enabled": bool,
            "default_processor": str,
            "processors": {
                "unstructured": {...},
                "tesseract": {...},
                "custom": {...},
            }
        }
    """
    config: dict[str, Any] = {
        "enabled": _dynaconf.get("ENABLE_DOCUMENT_PROCESSING"),
        "default_processor": _dynaconf.get("DOCUMENT_PROCESSOR"),
        "processors": {},
    }

    # Unstructured configuration
    if _dynaconf.get("ENABLE_UNSTRUCTURED"):
        languages_str = _dynaconf.get("UNSTRUCTURED_LANGUAGES")
        config["processors"]["unstructured"] = {
            "api_url": _dynaconf.get("UNSTRUCTURED_API_URL"),
            "timeout": _dynaconf.get("UNSTRUCTURED_TIMEOUT"),
            "strategy": _dynaconf.get("UNSTRUCTURED_STRATEGY"),
            "languages": [
                lang.strip() for lang in languages_str.split(",") if lang.strip()
            ],
            "progress_interval": _dynaconf.get("PROGRESS_INTERVAL"),
        }

    # Tesseract configuration
    if _dynaconf.get("ENABLE_TESSERACT"):
        config["processors"]["tesseract"] = {
            "tesseract_cmd": _dynaconf.get("TESSERACT_CMD"),  # None = auto-detect
            "lang": _dynaconf.get("TESSERACT_LANG"),
        }

    # PyMuPDF configuration (local PDF processing)
    if _dynaconf.get("ENABLE_PYMUPDF"):  # Enabled by default
        config["processors"]["pymupdf"] = {
            "extract_images": _dynaconf.get("PYMUPDF_EXTRACT_IMAGES"),
            "image_dir": _dynaconf.get(
                "PYMUPDF_IMAGE_DIR"
            ),  # None = use temp directory
        }

    # Custom processor (via HTTP API)
    if _dynaconf.get("ENABLE_CUSTOM_PROCESSOR"):
        custom_url = _dynaconf.get("CUSTOM_PROCESSOR_URL")
        if custom_url:
            supported_types_str = _dynaconf.get("CUSTOM_PROCESSOR_TYPES")
            supported_types = {
                t.strip() for t in supported_types_str.split(",") if t.strip()
            }

            config["processors"]["custom"] = {
                "name": _dynaconf.get("CUSTOM_PROCESSOR_NAME"),
                "api_url": custom_url,
                "api_key": _dynaconf.get("CUSTOM_PROCESSOR_API_KEY"),
                "timeout": _dynaconf.get("CUSTOM_PROCESSOR_TIMEOUT"),
                "supported_types": supported_types,
            }

    return config


@dataclass
class Settings:
    """Application settings from environment variables."""

    # Deployment mode (ADR-021: explicit mode selection)
    # Optional: If not set, mode is auto-detected from other settings
    # Valid values: single_user_basic, multi_user_basic, oauth_single_audience
    deployment_mode: str | None = None

    # OAuth/OIDC settings
    oidc_discovery_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_issuer: str | None = None

    # Nextcloud settings
    nextcloud_host: str | None = None
    nextcloud_username: str | None = None
    nextcloud_password: str | None = None
    nextcloud_app_password: str | None = None  # Preferred over nextcloud_password

    # Nextcloud SSL/TLS settings
    nextcloud_verify_ssl: bool = True
    nextcloud_ca_bundle: str | None = None

    # ADR-005: Token Audience Validation (required for OAuth mode)
    nextcloud_mcp_server_url: str | None = None  # MCP server URL (used as audience)
    nextcloud_resource_uri: str | None = None  # Nextcloud resource identifier

    # Token verification endpoints
    jwks_uri: str | None = None
    introspection_uri: str | None = None
    userinfo_uri: str | None = None

    # Progressive Consent settings (always enabled - no flag needed)
    enable_offline_access: bool = False

    # Multi-user BasicAuth pass-through mode (ADR-019 interim solution)
    # When enabled, MCP server extracts BasicAuth credentials from request headers
    # and passes them through to Nextcloud APIs (no storage, stateless)
    enable_multi_user_basic_auth: bool = False

    # Login Flow v2 settings (ADR-022)
    enable_login_flow: bool = False

    # Token and webhook storage settings
    # TOKEN_ENCRYPTION_KEY: Optional - Only required for OAuth token storage operations.
    #                       Webhook tracking works without encryption key.
    #                       If set, must be a valid base64-encoded Fernet key (32 bytes).
    # TOKEN_STORAGE_DB: Path to SQLite database for persistent storage.
    #                   Used for webhook tracking (all modes) and OAuth token storage.
    #                   Defaults to /tmp/tokens.db
    token_encryption_key: str | None = None
    token_storage_db: str | None = None

    # Vector sync settings (ADR-007)
    vector_sync_enabled: bool = False
    vector_sync_scan_interval: int = 300  # seconds (5 minutes)
    vector_sync_processor_workers: int = 3
    vector_sync_queue_max_size: int = 10000
    vector_sync_user_poll_interval: int = 60  # seconds - OAuth mode user discovery

    # Qdrant settings (mutually exclusive modes)
    qdrant_url: str | None = None  # Network mode: http://qdrant:6333
    qdrant_location: str | None = None  # Local mode: :memory: or /path/to/data
    qdrant_api_key: str | None = None
    qdrant_collection: str = "nextcloud_content"

    # Ollama settings (for embeddings)
    ollama_base_url: str | None = None
    ollama_embedding_model: str = "nomic-embed-text"
    ollama_verify_ssl: bool = True

    # OpenAI settings (for embeddings)
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_embedding_model: str = "text-embedding-3-small"

    # Document chunking settings (for vector embeddings)
    document_chunk_size: int = 2048  # Characters per chunk
    document_chunk_overlap: int = 200  # Overlapping characters between chunks

    # Observability settings
    metrics_enabled: bool = True
    metrics_port: int = 9090
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_verify_ssl: bool = False
    otel_service_name: str = "nextcloud-mcp-server"
    otel_traces_sampler: str = "always_on"
    otel_traces_sampler_arg: float = 1.0
    log_format: str = "text"  # "json" or "text"
    log_level: str = "INFO"
    log_include_trace_context: bool = True

    def __post_init__(self):
        """Validate configuration and set defaults."""
        logger = logging.getLogger(__name__)

        # Validate SSL/TLS configuration
        if not self.nextcloud_verify_ssl:
            logger.warning(
                "NEXTCLOUD_VERIFY_SSL is disabled. "
                "TLS certificate verification is turned off for all Nextcloud connections. "
                "This is insecure and should only be used for development/testing."
            )
        if self.nextcloud_ca_bundle:
            if not os.path.isfile(self.nextcloud_ca_bundle):
                raise ValueError(
                    f"NEXTCLOUD_CA_BUNDLE path does not exist: {self.nextcloud_ca_bundle}"
                )
            logger.info("Using custom CA bundle: %s", self.nextcloud_ca_bundle)

        # Ensure mutual exclusivity
        if self.qdrant_url and self.qdrant_location:
            raise ValueError(
                "Cannot set both QDRANT_URL and QDRANT_LOCATION. "
                "Use QDRANT_URL for network mode or QDRANT_LOCATION for local mode."
            )

        # Default to :memory: if neither set
        if not self.qdrant_url and not self.qdrant_location:
            self.qdrant_location = ":memory:"
            logger.debug("Using default Qdrant mode: in-memory (:memory:)")

        # Warn if API key set in local mode
        if self.qdrant_location and self.qdrant_api_key:
            logger.warning(
                "QDRANT_API_KEY is set but QDRANT_LOCATION is used (local mode). "
                "API key is only relevant for network mode and will be ignored."
            )

        # Validate chunking configuration
        if self.document_chunk_overlap >= self.document_chunk_size:
            raise ValueError(
                f"DOCUMENT_CHUNK_OVERLAP ({self.document_chunk_overlap}) must be less than "
                f"DOCUMENT_CHUNK_SIZE ({self.document_chunk_size}). "
                f"Overlap should be 10-20% of chunk size for optimal results."
            )

        if self.document_chunk_size < 512:
            logger.warning(
                f"DOCUMENT_CHUNK_SIZE is set to {self.document_chunk_size} characters, which is quite small. "
                f"Smaller chunks may lose context. Consider using at least 1024 characters."
            )

    def get_embedding_model_name(self) -> str:
        """
        Get the active embedding model name based on provider priority.

        Priority order (same as ProviderRegistry):
        1. OpenAI - if OPENAI_API_KEY is set
        2. Ollama - if OLLAMA_BASE_URL is set
        3. Simple - fallback (returns "simple-384")

        Returns:
            Active embedding model name
        """
        # Check OpenAI first (higher priority than Ollama in registry)
        if self.openai_api_key:
            return self.openai_embedding_model

        # Check Ollama
        if self.ollama_base_url:
            return self.ollama_embedding_model

        # Fallback to simple provider indicator
        return "simple-384"

    def get_collection_name(self) -> str:
        """
        Get Qdrant collection name.

        Auto-generates from deployment ID + model name unless explicitly set.
        Deployment ID uses OTEL_SERVICE_NAME if configured, otherwise hostname.

        This enables:
        - Safe embedding model switching (new model → new collection)
        - Multi-server deployments (unique deployment IDs)
        - Clear collection naming (shows deployment and model)

        Format: {deployment-id}-{model-name}

        Examples:
            - "my-deployment-nomic-embed-text" (Ollama)
            - "my-deployment-text-embedding-3-small" (OpenAI)
            - "mcp-container-openai-text-embedding-3-small" (hostname fallback)

        Returns:
            Collection name string
        """

        # Use explicit override if user configured non-default value
        if self.qdrant_collection != "nextcloud_content":
            return self.qdrant_collection

        # Determine deployment ID (OTEL service name or hostname fallback)
        if self.otel_service_name != "nextcloud-mcp-server":  # Non-default
            deployment_id = self.otel_service_name
        else:
            # Fallback to hostname for simple Docker deployments without OTEL config
            deployment_id = socket.gethostname()

        # Sanitize deployment ID and model name
        deployment_id = deployment_id.lower().replace(" ", "-").replace("_", "-")
        model_name = self.get_embedding_model_name().replace("/", "-").replace(":", "-")

        return f"{deployment_id}-{model_name}"

    # ADR-021: Property aliases for new naming convention
    # These provide the new names while maintaining backward compatibility with old field names

    @property
    def enable_semantic_search(self) -> bool:
        """Semantic search enabled (ADR-021 alias for vector_sync_enabled)."""
        return self.vector_sync_enabled

    @property
    def enable_background_operations(self) -> bool:
        """Background operations enabled (ADR-021 alias for enable_offline_access)."""
        return self.enable_offline_access


def _get_semantic_search_enabled() -> bool:
    """Get semantic search enabled status, supporting both old and new variable names.

    Supports:
    - ENABLE_SEMANTIC_SEARCH (new, preferred)
    - VECTOR_SYNC_ENABLED (old, deprecated)

    Returns:
        True if semantic search should be enabled
    """
    logger = logging.getLogger(__name__)

    new_value = _dynaconf.get("ENABLE_SEMANTIC_SEARCH", False)
    old_value = _dynaconf.get("VECTOR_SYNC_ENABLED", False)

    if new_value and old_value:
        logger.warning(
            "Both ENABLE_SEMANTIC_SEARCH and VECTOR_SYNC_ENABLED are set. "
            "Using ENABLE_SEMANTIC_SEARCH. "
            "VECTOR_SYNC_ENABLED is deprecated and will be removed in v1.0.0."
        )
    elif old_value and not new_value:
        logger.warning(
            "VECTOR_SYNC_ENABLED is deprecated. "
            "Please use ENABLE_SEMANTIC_SEARCH instead. "
            "Support for VECTOR_SYNC_ENABLED will be removed in v1.0.0."
        )

    return new_value or old_value


def _is_multi_user_mode() -> bool:
    """Detect if this is a multi-user deployment mode.

    Multi-user modes are:
    - Multi-user BasicAuth (ENABLE_MULTI_USER_BASIC_AUTH=true)
    - OAuth Single-Audience (no username/password set)
    - OAuth Token Exchange (ENABLE_TOKEN_EXCHANGE=true)

    Single-user modes are:
    - Single-user BasicAuth (username and password both set)

    Returns:
        True if multi-user mode detected
    """
    # Multi-user BasicAuth explicitly enabled
    if _dynaconf.get("ENABLE_MULTI_USER_BASIC_AUTH", False):
        return True

    # Token exchange implies OAuth multi-user
    if _dynaconf.get("ENABLE_TOKEN_EXCHANGE", False):
        return True

    # If both username and password are set, it's single-user BasicAuth
    has_username = bool(_dynaconf.get("NEXTCLOUD_USERNAME"))
    has_password = bool(_dynaconf.get("NEXTCLOUD_PASSWORD"))
    if has_username and has_password:
        return False

    # Otherwise, assume OAuth multi-user (default when no credentials provided)
    return True


def _get_background_operations_enabled() -> bool:
    """Get background operations enabled status with auto-enablement for semantic search.

    Supports:
    - ENABLE_BACKGROUND_OPERATIONS (new, preferred)
    - ENABLE_OFFLINE_ACCESS (old, deprecated)
    - Auto-enabled if ENABLE_SEMANTIC_SEARCH=true in multi-user modes

    Returns:
        True if background operations should be enabled
    """
    logger = logging.getLogger(__name__)

    # Check new and old variable names
    explicit = _dynaconf.get("ENABLE_BACKGROUND_OPERATIONS", False)
    legacy = _dynaconf.get("ENABLE_OFFLINE_ACCESS", False)

    if explicit and legacy:
        logger.warning(
            "Both ENABLE_BACKGROUND_OPERATIONS and ENABLE_OFFLINE_ACCESS are set. "
            "Using ENABLE_BACKGROUND_OPERATIONS. "
            "ENABLE_OFFLINE_ACCESS is deprecated and will be removed in v1.0.0."
        )
    elif legacy and not explicit:
        logger.warning(
            "ENABLE_OFFLINE_ACCESS is deprecated. "
            "Please use ENABLE_BACKGROUND_OPERATIONS instead. "
            "Support for ENABLE_OFFLINE_ACCESS will be removed in v1.0.0."
        )

    # Auto-enable if semantic search is enabled in multi-user mode
    semantic_search_enabled = _get_semantic_search_enabled()
    is_multi_user = _is_multi_user_mode()
    auto_enabled = semantic_search_enabled and is_multi_user

    if auto_enabled and not (explicit or legacy):
        logger.info(
            "Automatically enabled background operations for semantic search in multi-user mode. "
            "Set ENABLE_BACKGROUND_OPERATIONS=false to disable (this will also disable semantic search)."
        )

    return explicit or legacy or auto_enabled


def _dget(key):
    """Get a value from dynaconf if configured, otherwise return _UNSET.

    Distinguishes "explicitly set to None" (via @none in TOML or env var)
    from "not configured at all". When _UNSET is returned, callers should
    let the Settings dataclass default apply.
    """
    return _dynaconf[key] if key in _dynaconf else _UNSET


def get_settings() -> Settings:
    """Get application settings from dynaconf configuration.

    Settings are loaded from (last wins):
    1. settings.toml [default] section
    2. settings.toml [<mode>] section (via MCP_DEPLOYMENT_MODE)
    3. .secrets.toml (if present)
    4. settings.local.toml (if present)
    5. Environment variables (highest priority)

    Values not found in any source are omitted, letting Settings dataclass
    defaults apply. This ensures the server starts correctly even without
    settings.toml (e.g., env-var-only deployments).

    Returns:
        Settings object with configuration values
    """
    # Get consolidated values with smart dependency resolution
    enable_semantic_search = _get_semantic_search_enabled()
    enable_background_operations = _get_background_operations_enabled()

    # Mapping from Settings field name to dynaconf key
    _field_map = {
        # Deployment mode (ADR-021)
        "deployment_mode": "MCP_DEPLOYMENT_MODE",
        # OAuth/OIDC settings
        "oidc_discovery_url": "OIDC_DISCOVERY_URL",
        "oidc_client_id": "NEXTCLOUD_OIDC_CLIENT_ID",
        "oidc_client_secret": "NEXTCLOUD_OIDC_CLIENT_SECRET",
        "oidc_issuer": "OIDC_ISSUER",
        # Nextcloud settings
        "nextcloud_host": "NEXTCLOUD_HOST",
        "nextcloud_username": "NEXTCLOUD_USERNAME",
        "nextcloud_password": "NEXTCLOUD_PASSWORD",
        "nextcloud_app_password": "NEXTCLOUD_APP_PASSWORD",
        # Nextcloud SSL/TLS settings
        "nextcloud_verify_ssl": "NEXTCLOUD_VERIFY_SSL",
        "nextcloud_ca_bundle": "NEXTCLOUD_CA_BUNDLE",
        # ADR-005: Token Audience Validation
        "nextcloud_mcp_server_url": "NEXTCLOUD_MCP_SERVER_URL",
        "nextcloud_resource_uri": "NEXTCLOUD_RESOURCE_URI",
        # Token verification endpoints
        "jwks_uri": "JWKS_URI",
        "introspection_uri": "INTROSPECTION_URI",
        "userinfo_uri": "USERINFO_URI",
        # Multi-user BasicAuth pass-through mode
        "enable_multi_user_basic_auth": "ENABLE_MULTI_USER_BASIC_AUTH",
        # Login Flow v2 settings (ADR-022)
        "enable_login_flow": "ENABLE_LOGIN_FLOW",
        # Token and webhook storage settings
        "token_encryption_key": "TOKEN_ENCRYPTION_KEY",
        "token_storage_db": "TOKEN_STORAGE_DB",
        # Vector sync settings (ADR-007)
        "vector_sync_scan_interval": "VECTOR_SYNC_SCAN_INTERVAL",
        "vector_sync_processor_workers": "VECTOR_SYNC_PROCESSOR_WORKERS",
        "vector_sync_queue_max_size": "VECTOR_SYNC_QUEUE_MAX_SIZE",
        "vector_sync_user_poll_interval": "VECTOR_SYNC_USER_POLL_INTERVAL",
        # Qdrant settings
        "qdrant_url": "QDRANT_URL",
        "qdrant_location": "QDRANT_LOCATION",
        "qdrant_api_key": "QDRANT_API_KEY",
        "qdrant_collection": "QDRANT_COLLECTION",
        # Ollama settings
        "ollama_base_url": "OLLAMA_BASE_URL",
        "ollama_embedding_model": "OLLAMA_EMBEDDING_MODEL",
        "ollama_verify_ssl": "OLLAMA_VERIFY_SSL",
        # OpenAI settings
        "openai_api_key": "OPENAI_API_KEY",
        "openai_base_url": "OPENAI_BASE_URL",
        "openai_embedding_model": "OPENAI_EMBEDDING_MODEL",
        # Document chunking settings
        "document_chunk_size": "DOCUMENT_CHUNK_SIZE",
        "document_chunk_overlap": "DOCUMENT_CHUNK_OVERLAP",
        # Observability settings
        "metrics_enabled": "METRICS_ENABLED",
        "metrics_port": "METRICS_PORT",
        "otel_exporter_otlp_endpoint": "OTEL_EXPORTER_OTLP_ENDPOINT",
        "otel_exporter_verify_ssl": "OTEL_EXPORTER_VERIFY_SSL",
        "otel_service_name": "OTEL_SERVICE_NAME",
        "otel_traces_sampler": "OTEL_TRACES_SAMPLER",
        "otel_traces_sampler_arg": "OTEL_TRACES_SAMPLER_ARG",
        "log_format": "LOG_FORMAT",
        "log_level": "LOG_LEVEL",
        "log_include_trace_context": "LOG_INCLUDE_TRACE_CONTEXT",
    }

    # Only pass values that dynaconf actually has; omit unset keys so
    # the Settings dataclass defaults apply.
    kwargs = {
        field: val
        for field, key in _field_map.items()
        if (val := _dget(key)) is not _UNSET
    }

    # Smart dependency overrides (always set, regardless of dynaconf)
    kwargs["vector_sync_enabled"] = enable_semantic_search
    kwargs["enable_offline_access"] = enable_background_operations

    return Settings(**kwargs)


def get_nextcloud_ssl_verify() -> bool | ssl.SSLContext:
    """Return the SSL verification setting for Nextcloud connections.

    Returns:
        - False if NEXTCLOUD_VERIFY_SSL=false (disable verification)
        - ssl.SSLContext if NEXTCLOUD_CA_BUNDLE is set (custom CA)
        - True otherwise (default system CA verification)
    """
    settings = get_settings()
    if not settings.nextcloud_verify_ssl:
        return False
    if settings.nextcloud_ca_bundle:
        ctx = ssl.create_default_context(cafile=settings.nextcloud_ca_bundle)
        return ctx
    return True
