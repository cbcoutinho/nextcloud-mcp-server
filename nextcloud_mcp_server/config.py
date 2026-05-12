import atexit
import logging
import logging.config
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dynaconf import Dynaconf, Validator

# Sentinel for "key not in dynaconf at all" vs "explicitly set to None".
_UNSET = object()

# Built-in defaults — declared in Python so env vars work without any settings
# file being present (e.g., `uvx` / `pip install` deployments). Mirrors the
# [default] section that used to live in settings.toml. Keys set here are
# "known" to dynaconf, which is required because we run with
# ignore_unknown_envvars=True. See ADR-024/025.
_DEFAULTS: dict[str, Any] = {
    # Deployment mode (ADR-021)
    "mcp_deployment_mode": None,
    # Nextcloud core
    "nextcloud_host": None,
    "nextcloud_username": None,
    "nextcloud_password": None,
    "nextcloud_app_password": None,
    "nextcloud_verify_ssl": True,
    "nextcloud_ca_bundle": None,
    "nextcloud_mcp_server_url": None,
    "nextcloud_resource_uri": None,
    "nextcloud_public_issuer_url": None,
    "cookie_secure": None,
    # OAuth/OIDC
    "oidc_discovery_url": None,
    "nextcloud_oidc_client_id": None,
    "nextcloud_oidc_client_secret": None,
    "oidc_issuer": None,
    "jwks_uri": None,
    "introspection_uri": None,
    "userinfo_uri": None,
    "oidc_resource_server_id": None,
    # Mode flags
    # NOTE: `enable_multi_user_basic_auth` and `enable_login_flow` are
    # intentionally absent — they are derived from MCP_DEPLOYMENT_MODE in
    # Settings.__post_init__ (ADR-022) and not read from the dynaconf store.
    "enable_semantic_search": False,
    "enable_background_operations": False,
    "vector_sync_enabled": False,
    "enable_offline_access": False,
    "enable_token_exchange": False,
    # Token storage
    "token_encryption_key": None,
    # None = ephemeral per-process tempfile (see get_token_db_path()).
    # Set TOKEN_STORAGE_DB to persist tokens across restarts.
    "token_storage_db": None,
    # Webhook delivery authentication (ADR-010): when set, registrations
    # tell NC to add `Authorization: Bearer <secret>` to webhook deliveries
    # and the receiver rejects unauthenticated requests.
    "webhook_secret": None,
    # Internal URL override for webhook registration; wins over
    # NEXTCLOUD_MCP_SERVER_URL when set (e.g. split internal/external URLs).
    "webhook_internal_url": None,
    # Vector sync
    "vector_sync_scan_interval": 300,
    "vector_sync_processor_workers": 3,
    "vector_sync_queue_max_size": 10000,
    "vector_sync_user_poll_interval": 60,
    # Verify-on-read concurrency cap (ADR-019)
    "verification_concurrency": 20,
    # Qdrant
    "qdrant_url": None,
    "qdrant_location": None,
    "qdrant_api_key": None,
    "qdrant_collection": "nextcloud_content",
    # Ollama
    "ollama_base_url": None,
    "ollama_embedding_model": "nomic-embed-text",
    "ollama_generation_model": None,
    "ollama_verify_ssl": True,
    # OpenAI
    "openai_api_key": None,
    "openai_base_url": None,
    "openai_embedding_model": "text-embedding-3-small",
    "openai_generation_model": None,
    # Bedrock (AWS)
    "aws_region": None,
    "aws_access_key_id": None,
    "aws_secret_access_key": None,
    "bedrock_embedding_model": None,
    "bedrock_generation_model": None,
    # Mistral
    "mistral_api_key": None,
    "mistral_embedding_model": "mistral-embed",
    "mistral_base_url": None,
    # Simple (fallback) embedding dimension
    "simple_embedding_dimension": 384,
    # Document chunking
    "document_chunk_size": 2048,
    "document_chunk_overlap": 200,
    # Observability
    "metrics_enabled": True,
    "metrics_port": 9090,
    "otel_exporter_otlp_endpoint": None,
    "otel_exporter_verify_ssl": False,
    "otel_service_name": "nextcloud-mcp-server",
    "otel_traces_sampler": "always_on",
    "otel_traces_sampler_arg": 1.0,
    "log_format": "text",
    "log_level": "INFO",
    "log_include_trace_context": True,
    # Document processing
    "enable_document_processing": False,
    "document_processor": "unstructured",
    "enable_unstructured": False,
    "unstructured_api_url": "http://unstructured:8000",
    "unstructured_timeout": 120,
    "unstructured_strategy": "auto",
    "unstructured_languages": "eng,deu",
    "progress_interval": 10,
    "enable_tesseract": False,
    "tesseract_cmd": None,
    "tesseract_lang": "eng",
    "enable_pymupdf": True,
    "pymupdf_extract_images": True,
    "pymupdf_image_dir": None,
    "enable_custom_processor": False,
    "custom_processor_url": None,
    "custom_processor_types": "application/pdf",
    "custom_processor_name": "custom",
    "custom_processor_api_key": None,
    "custom_processor_timeout": 60,
    # Tag-based file exclusion (issue #710): comma-separated list of
    # Nextcloud system tag names. Files/folders carrying any of these tags
    # are hidden from WebDAV MCP tools. Empty = feature off.
    "excluded_tags": "",
}


def _resolve_settings_files() -> list[str]:
    """Find optional external settings files.

    Priority:
      1. NEXTCLOUD_MCP_SETTINGS_FILE env var (absolute or relative path).
         If set but the file does not exist, raise FileNotFoundError —
         silently falling back to defaults on a typo would be a footgun.
         .secrets.toml is looked for alongside the explicit file.
      2. Otherwise ./settings.toml in cwd (for docker / dev workflows),
         with .secrets.toml also looked for in cwd.

    Returns an empty list if nothing is configured — that's fine, defaults
    and env vars still apply.
    """
    files: list[str] = []
    explicit = os.environ.get("NEXTCLOUD_MCP_SETTINGS_FILE")
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(
                f"NEXTCLOUD_MCP_SETTINGS_FILE points to a file that does "
                f"not exist: {explicit}"
            )
        files.append(str(p))
        secrets = p.parent / ".secrets.toml"
    else:
        cwd_settings = Path.cwd() / "settings.toml"
        if cwd_settings.exists():
            files.append(str(cwd_settings))
        secrets = Path.cwd() / ".secrets.toml"
    if secrets.exists():
        files.append(str(secrets))
    return files


# Dynaconf instance — env vars always win (12-factor). Settings files are
# optional; when absent the defaults above provide the full key schema so
# env vars still override correctly. See ADR-024/025 for architecture.
_dynaconf = Dynaconf(
    settings_files=_resolve_settings_files(),
    environments=True,
    envvar_prefix=False,
    env_switcher="MCP_DEPLOYMENT_MODE",
    ignore_unknown_envvars=True,
    load_dotenv=False,
    **_DEFAULTS,
    validators=[
        # Port ranges
        Validator("METRICS_PORT", gte=1, lte=65535),
        # Positive integers
        Validator("VECTOR_SYNC_SCAN_INTERVAL", gte=1),
        Validator("VECTOR_SYNC_PROCESSOR_WORKERS", gte=1),
        Validator("VECTOR_SYNC_QUEUE_MAX_SIZE", gte=1),
        Validator("VECTOR_SYNC_USER_POLL_INTERVAL", gte=1),
        Validator("VERIFICATION_CONCURRENCY", gte=1),
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


_ephemeral_db_path: str | None = None


def get_token_db_path() -> str:
    """Resolve the token SQLite database path.

    Priority:
    1. TOKEN_STORAGE_DB if explicitly set — docker-compose pins
       /app/data/tokens.db this way. Read via dynaconf, which picks up
       the env var because TOKEN_STORAGE_DB is declared in _DEFAULTS.
    2. Otherwise a per-process tempfile under tempfile.gettempdir(),
       allocated lazily and deleted at interpreter exit via atexit.
       Ephemeral: tokens are wiped on restart, matching the Qdrant
       ":memory:" default pattern used elsewhere in this project.
    """
    explicit = _dynaconf.get("TOKEN_STORAGE_DB")
    if explicit:
        return str(explicit)
    global _ephemeral_db_path
    if _ephemeral_db_path is None:
        fd, path = tempfile.mkstemp(
            prefix=f"nextcloud-mcp-tokens-{os.getpid()}-", suffix=".db"
        )
        os.close(fd)
        _ephemeral_db_path = path

        def _cleanup(p: str = path) -> None:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass

        atexit.register(_cleanup)
    return _ephemeral_db_path


def is_ephemeral_token_db(path: str) -> bool:
    """Return True if the given path is the process-local ephemeral tempfile.

    Precondition: `get_token_db_path()` must have been called at least once
    in this process to allocate the tempfile. If called before allocation,
    this returns False for any input (including the eventual tempfile path),
    because there is nothing to compare against yet. In practice every call
    site in this repo resolves the path via `get_token_db_path()` first.
    """
    return path == _ephemeral_db_path


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

    # Deployment mode (ADR-021: explicit mode selection; updated by ADR-022)
    # Optional: If not set, mode is auto-detected from other settings
    # Valid values: single_user_basic, multi_user_basic, login_flow
    # (ADR-022: `oauth_single_audience` was renamed to `login_flow`.)
    deployment_mode: str | None = None

    # OAuth/OIDC settings
    oidc_discovery_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_issuer: str | None = None
    oidc_resource_server_id: str | None = None

    # Nextcloud settings
    nextcloud_host: str | None = None
    nextcloud_username: str | None = None
    nextcloud_password: str | None = None
    nextcloud_app_password: str | None = None  # Preferred over nextcloud_password

    # Browser-reachable public URL for OAuth/Login-Flow-v2 redirects when
    # NEXTCLOUD_HOST is an internal Docker hostname. Falls back to
    # nextcloud_host when unset.
    nextcloud_public_issuer_url: str | None = None

    # Browser cookie Secure flag. None = auto-detect from nextcloud_host
    # scheme (https → True, else False). Set COOKIE_SECURE=true/false to
    # override.
    cookie_secure: bool | None = None

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

    # Multi-user BasicAuth pass-through mode (ADR-019 interim solution).
    # Internal — not user-settable; the ENABLE_MULTI_USER_BASIC_AUTH env-var
    # alias was removed in the ADR-022 follow-up. Auto-set by
    # Settings.__post_init__ when MCP_DEPLOYMENT_MODE=multi_user_basic. When True,
    # the MCP server extracts BasicAuth credentials from request headers and
    # passes them through to Nextcloud APIs (no storage, stateless). Kept
    # as a field for backward compat with the runtime call sites that read it.
    enable_multi_user_basic_auth: bool = False

    # Login Flow v2 derived flag (ADR-022). Internal — not user-settable.
    # Auto-set by Settings.__post_init__ when the resolved deployment mode is
    # LOGIN_FLOW. Kept as a field for backward compat with the runtime call
    # sites that read it (app.py, context.py, scope_authorization.py).
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

    # Webhook delivery authentication (ADR-010).
    # When set, the registrar passes Authorization: Bearer <secret> as the
    # webhook authData and the receiver validates the same header on each
    # delivery. When unset, registration uses authMethod="none" and the
    # receiver accepts unauthenticated POSTs (backward-compatible).
    webhook_secret: str | None = None
    # Internal URL override for webhook registration. Highest-priority
    # source for the URL we register with NC (above
    # nextcloud_mcp_server_url and the docker-detection fallback).
    webhook_internal_url: str | None = None

    # Vector sync settings (ADR-007)
    vector_sync_enabled: bool = False
    vector_sync_scan_interval: int = 300  # seconds (5 minutes)
    vector_sync_processor_workers: int = 3
    vector_sync_queue_max_size: int = 10000
    vector_sync_user_poll_interval: int = 60  # seconds - OAuth mode user discovery

    # Verify-on-read concurrency (ADR-019). Cap on parallel Nextcloud
    # round-trips during search-result verification fan-out. Lower this if the
    # Nextcloud backend struggles with the parallel load; raise it on a
    # healthy connection to speed up large result pages.
    verification_concurrency: int = 20

    # Qdrant settings (mutually exclusive modes)
    qdrant_url: str | None = None  # Network mode: http://qdrant:6333
    qdrant_location: str | None = None  # Local mode: :memory: or /path/to/data
    qdrant_api_key: str | None = None
    qdrant_collection: str = "nextcloud_content"

    # Ollama settings (embeddings + optional generation)
    ollama_base_url: str | None = None
    ollama_embedding_model: str = "nomic-embed-text"
    ollama_generation_model: str | None = None
    ollama_verify_ssl: bool = True

    # OpenAI settings (embeddings + optional generation)
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_embedding_model: str = "text-embedding-3-small"
    openai_generation_model: str | None = None

    # Bedrock (AWS) settings — boto3 also reads these from its credential chain
    aws_region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    bedrock_embedding_model: str | None = None
    bedrock_generation_model: str | None = None

    # Mistral settings (embeddings only)
    mistral_api_key: str | None = None
    mistral_embedding_model: str = "mistral-embed"
    mistral_base_url: str | None = None

    # Simple (fallback) provider — dimension when no real provider configured
    simple_embedding_dimension: int = 384

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

    # Tag-based file exclusion (issue #710): comma-separated list of
    # Nextcloud system tag names. Files/folders carrying any of these tags
    # are hidden from WebDAV MCP tools.
    excluded_tags: str = ""

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
                "DOCUMENT_CHUNK_SIZE is set to %s characters, which is quite small. Smaller chunks may lose context. Consider using at least 1024 characters.",
                self.document_chunk_size,
            )

        # --- ADR-022 follow-up: deployment mode is the single source of truth ---
        # The ENABLE_MULTI_USER_BASIC_AUTH and ENABLE_LOGIN_FLOW env vars were
        # removed in favour of MCP_DEPLOYMENT_MODE. We do TWO things here:
        #
        # 1. Loud-fail if a user still has either legacy env var set to a
        #    truthy value (silent removal would have flipped them into the
        #    wrong runtime mode). Only fires for truthy strings, so an
        #    explicit `ENABLE_LOGIN_FLOW=false` in a leftover .env passes
        #    through harmlessly.
        # 2. Derive `enable_login_flow` and `enable_multi_user_basic_auth`
        #    from the resolved deployment mode here, in __post_init__, so
        #    every Settings instance carries correct flags. (`get_settings()`
        #    builds a fresh Settings on each call — without this, the
        #    mutation that used to live in detect_auth_mode would only stick
        #    on the startup Settings instance, leaving per-request handlers
        #    with default False values.)
        _truthy = {"1", "true", "yes", "on"}
        for _legacy, _replacement in (
            ("ENABLE_MULTI_USER_BASIC_AUTH", "multi_user_basic"),
            ("ENABLE_LOGIN_FLOW", "login_flow"),
        ):
            if os.environ.get(_legacy, "").strip().lower() in _truthy:
                raise ValueError(
                    f"{_legacy} is no longer read from the environment. "
                    f"Set MCP_DEPLOYMENT_MODE={_replacement} instead "
                    "(ADR-022). The deployment mode is the single source "
                    "of truth for selecting an auth flow."
                )

        # NOTE: this block mirrors the resolution logic in
        # `config_validators.detect_auth_mode` (which works on strings via a
        # `mode_map`). Both call sites resolve the deployment mode
        # independently — the canonical AuthMode enum in detect_auth_mode,
        # and the boolean derived flags here. **Keep them in sync when
        # adding a new mode**: a new entry must be added in both places, in
        # addition to `mode_map` (`config_validators.py`) and any
        # MODE_REQUIREMENTS entry.
        resolved_mode = (self.deployment_mode or "").strip().lower()
        if not resolved_mode:
            if self.nextcloud_username and self.nextcloud_password:
                resolved_mode = "single_user_basic"
            else:
                # Default multi-user mode is Login Flow v2 (browser-based
                # app-password acquisition); the un-augmented OAuth bearer
                # pass-through it replaced needed unmerged Nextcloud
                # user_oidc patches and is no longer supported.
                resolved_mode = "login_flow"

        self.enable_multi_user_basic_auth = resolved_mode == "multi_user_basic"
        self.enable_login_flow = resolved_mode == "login_flow"

    def get_embedding_model_name(self) -> str:
        """
        Get the active embedding model name based on provider priority.

        Priority order (same as ProviderRegistry):
        1. Bedrock - if AWS_REGION or BEDROCK_EMBEDDING_MODEL is set
        2. OpenAI - if OPENAI_API_KEY is set
        3. Mistral - if MISTRAL_API_KEY is set
        4. Ollama - if OLLAMA_BASE_URL is set
        5. Simple - fallback (returns "simple-{dimension}")

        Returns:
            Active embedding model name
        """
        if (
            self.aws_region
            or self.bedrock_embedding_model
            or self.bedrock_generation_model
        ):
            return self.bedrock_embedding_model or "bedrock-default"

        if self.openai_api_key:
            return self.openai_embedding_model

        if self.mistral_api_key:
            return self.mistral_embedding_model

        if self.ollama_base_url:
            return self.ollama_embedding_model

        return f"simple-{self.simple_embedding_dimension}"

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

    Runs early in config setup (before Settings is fully built) for
    mode-conditional defaults. Must match the canonical detection in
    `config_validators.detect_auth_mode`, but works directly against the
    raw dynaconf store since Settings doesn't exist yet.

    Multi-user modes are:
    - Multi-user BasicAuth (MCP_DEPLOYMENT_MODE=multi_user_basic)
    - Login Flow v2 / default OAuth (MCP_DEPLOYMENT_MODE=login_flow, or no
      username/password and no explicit mode)
    - OAuth Token Exchange (ENABLE_TOKEN_EXCHANGE=true)

    Single-user mode is:
    - Single-user BasicAuth (username and password both set)

    Returns:
        True if multi-user mode detected
    """
    # Explicit deployment mode wins. The ENABLE_MULTI_USER_BASIC_AUTH env-var
    # alias was removed in the ADR-022 follow-up; selection is now via
    # MCP_DEPLOYMENT_MODE.
    explicit_mode = str(_dynaconf.get("MCP_DEPLOYMENT_MODE", "") or "").lower().strip()
    if explicit_mode in {"multi_user_basic", "login_flow"}:
        return True
    if explicit_mode == "single_user_basic":
        return False

    # Token exchange implies OAuth multi-user
    if _dynaconf.get("ENABLE_TOKEN_EXCHANGE", False):
        return True

    # If both username and password are set, it's single-user BasicAuth
    has_username = bool(_dynaconf.get("NEXTCLOUD_USERNAME"))
    has_password = bool(_dynaconf.get("NEXTCLOUD_PASSWORD"))
    if has_username and has_password:
        return False

    # Otherwise, assume multi-user (default when no credentials provided)
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
        "oidc_resource_server_id": "OIDC_RESOURCE_SERVER_ID",
        # Nextcloud settings
        "nextcloud_host": "NEXTCLOUD_HOST",
        "nextcloud_username": "NEXTCLOUD_USERNAME",
        "nextcloud_password": "NEXTCLOUD_PASSWORD",
        "nextcloud_app_password": "NEXTCLOUD_APP_PASSWORD",
        "nextcloud_public_issuer_url": "NEXTCLOUD_PUBLIC_ISSUER_URL",
        "cookie_secure": "COOKIE_SECURE",
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
        # NOTE: `enable_multi_user_basic_auth` and `enable_login_flow` no
        # longer have env-var aliases — both are derived from the resolved
        # MCP_DEPLOYMENT_MODE in detect_auth_mode() so users only configure
        # the mode (ADR-022 follow-up).
        # Token and webhook storage settings
        "token_encryption_key": "TOKEN_ENCRYPTION_KEY",
        "token_storage_db": "TOKEN_STORAGE_DB",
        # Webhook auth (ADR-010)
        "webhook_secret": "WEBHOOK_SECRET",
        "webhook_internal_url": "WEBHOOK_INTERNAL_URL",
        # Vector sync settings (ADR-007)
        "vector_sync_scan_interval": "VECTOR_SYNC_SCAN_INTERVAL",
        "vector_sync_processor_workers": "VECTOR_SYNC_PROCESSOR_WORKERS",
        "vector_sync_queue_max_size": "VECTOR_SYNC_QUEUE_MAX_SIZE",
        "vector_sync_user_poll_interval": "VECTOR_SYNC_USER_POLL_INTERVAL",
        # Verify-on-read (ADR-019)
        "verification_concurrency": "VERIFICATION_CONCURRENCY",
        # Qdrant settings
        "qdrant_url": "QDRANT_URL",
        "qdrant_location": "QDRANT_LOCATION",
        "qdrant_api_key": "QDRANT_API_KEY",
        "qdrant_collection": "QDRANT_COLLECTION",
        # Ollama settings
        "ollama_base_url": "OLLAMA_BASE_URL",
        "ollama_embedding_model": "OLLAMA_EMBEDDING_MODEL",
        "ollama_generation_model": "OLLAMA_GENERATION_MODEL",
        "ollama_verify_ssl": "OLLAMA_VERIFY_SSL",
        # OpenAI settings
        "openai_api_key": "OPENAI_API_KEY",
        "openai_base_url": "OPENAI_BASE_URL",
        "openai_embedding_model": "OPENAI_EMBEDDING_MODEL",
        "openai_generation_model": "OPENAI_GENERATION_MODEL",
        # Bedrock (AWS) settings
        "aws_region": "AWS_REGION",
        "aws_access_key_id": "AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
        "bedrock_embedding_model": "BEDROCK_EMBEDDING_MODEL",
        "bedrock_generation_model": "BEDROCK_GENERATION_MODEL",
        # Mistral settings
        "mistral_api_key": "MISTRAL_API_KEY",
        "mistral_embedding_model": "MISTRAL_EMBEDDING_MODEL",
        "mistral_base_url": "MISTRAL_BASE_URL",
        # Simple provider
        "simple_embedding_dimension": "SIMPLE_EMBEDDING_DIMENSION",
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
        "excluded_tags": "EXCLUDED_TAGS",
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
