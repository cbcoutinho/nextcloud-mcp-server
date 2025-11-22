import logging
import logging.config
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class DeploymentMode(Enum):
    """Deployment mode for the MCP server.

    SELF_HOSTED: Full features, environment-based configuration.
                 Supports vector sync, semantic search, admin UI.

    SMITHERY_STATELESS: Stateless mode for Smithery hosting.
                        Session-based configuration, no persistent storage.
                        Excludes semantic search, vector sync, admin UI.
    """

    SELF_HOSTED = "self_hosted"
    SMITHERY_STATELESS = "smithery"


def get_deployment_mode() -> DeploymentMode:
    """Detect deployment mode from environment.

    Returns:
        DeploymentMode.SMITHERY_STATELESS if SMITHERY_DEPLOYMENT=true,
        otherwise DeploymentMode.SELF_HOSTED (default).
    """
    if os.getenv("SMITHERY_DEPLOYMENT", "false").lower() == "true":
        return DeploymentMode.SMITHERY_STATELESS
    return DeploymentMode.SELF_HOSTED


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
    """Get document processor configuration from environment.

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
        "enabled": os.getenv("ENABLE_DOCUMENT_PROCESSING", "false").lower() == "true",
        "default_processor": os.getenv("DOCUMENT_PROCESSOR", "unstructured"),
        "processors": {},
    }

    # Unstructured configuration
    if os.getenv("ENABLE_UNSTRUCTURED", "false").lower() == "true":
        config["processors"]["unstructured"] = {
            "api_url": os.getenv("UNSTRUCTURED_API_URL", "http://unstructured:8000"),
            "timeout": int(os.getenv("UNSTRUCTURED_TIMEOUT", "120")),
            "strategy": os.getenv("UNSTRUCTURED_STRATEGY", "auto"),
            "languages": [
                lang.strip()
                for lang in os.getenv("UNSTRUCTURED_LANGUAGES", "eng,deu").split(",")
                if lang.strip()
            ],
            "progress_interval": int(os.getenv("PROGRESS_INTERVAL", "10")),
        }

    # Tesseract configuration
    if os.getenv("ENABLE_TESSERACT", "false").lower() == "true":
        config["processors"]["tesseract"] = {
            "tesseract_cmd": os.getenv("TESSERACT_CMD"),  # None = auto-detect
            "lang": os.getenv("TESSERACT_LANG", "eng"),
        }

    # PyMuPDF configuration (local PDF processing)
    if os.getenv("ENABLE_PYMUPDF", "true").lower() == "true":  # Enabled by default
        config["processors"]["pymupdf"] = {
            "extract_images": os.getenv("PYMUPDF_EXTRACT_IMAGES", "true").lower()
            == "true",
            "image_dir": os.getenv("PYMUPDF_IMAGE_DIR"),  # None = use temp directory
        }

    # Custom processor (via HTTP API)
    if os.getenv("ENABLE_CUSTOM_PROCESSOR", "false").lower() == "true":
        custom_url = os.getenv("CUSTOM_PROCESSOR_URL")
        if custom_url:
            supported_types_str = os.getenv("CUSTOM_PROCESSOR_TYPES", "application/pdf")
            supported_types = {
                t.strip() for t in supported_types_str.split(",") if t.strip()
            }

            config["processors"]["custom"] = {
                "name": os.getenv("CUSTOM_PROCESSOR_NAME", "custom"),
                "api_url": custom_url,
                "api_key": os.getenv("CUSTOM_PROCESSOR_API_KEY"),
                "timeout": int(os.getenv("CUSTOM_PROCESSOR_TIMEOUT", "60")),
                "supported_types": supported_types,
            }

    return config


@dataclass
class Settings:
    """Application settings from environment variables."""

    # OAuth/OIDC settings
    oidc_discovery_url: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_issuer: Optional[str] = None

    # Nextcloud settings
    nextcloud_host: Optional[str] = None
    nextcloud_username: Optional[str] = None
    nextcloud_password: Optional[str] = None

    # ADR-005: Token Audience Validation (required for OAuth mode)
    nextcloud_mcp_server_url: Optional[str] = None  # MCP server URL (used as audience)
    nextcloud_resource_uri: Optional[str] = None  # Nextcloud resource identifier

    # Token verification endpoints
    jwks_uri: Optional[str] = None
    introspection_uri: Optional[str] = None
    userinfo_uri: Optional[str] = None

    # Progressive Consent settings (always enabled - no flag needed)
    enable_token_exchange: bool = False
    enable_offline_access: bool = False

    # Token exchange cache settings
    token_exchange_cache_ttl: int = 300  # seconds (5 minutes default)

    # Token and webhook storage settings
    # TOKEN_ENCRYPTION_KEY: Optional - Only required for OAuth token storage operations.
    #                       Webhook tracking works without encryption key.
    #                       If set, must be a valid base64-encoded Fernet key (32 bytes).
    # TOKEN_STORAGE_DB: Path to SQLite database for persistent storage.
    #                   Used for webhook tracking (all modes) and OAuth token storage.
    #                   Defaults to /tmp/tokens.db
    token_encryption_key: Optional[str] = None
    token_storage_db: Optional[str] = None

    # Vector sync settings (ADR-007)
    vector_sync_enabled: bool = False
    vector_sync_scan_interval: int = 300  # seconds (5 minutes)
    vector_sync_processor_workers: int = 3
    vector_sync_queue_max_size: int = 10000

    # Qdrant settings (mutually exclusive modes)
    qdrant_url: Optional[str] = None  # Network mode: http://qdrant:6333
    qdrant_location: Optional[str] = None  # Local mode: :memory: or /path/to/data
    qdrant_api_key: Optional[str] = None
    qdrant_collection: str = "nextcloud_content"

    # Ollama settings (for embeddings)
    ollama_base_url: Optional[str] = None
    ollama_embedding_model: str = "nomic-embed-text"
    ollama_verify_ssl: bool = True

    # OpenAI settings (for embeddings)
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_embedding_model: str = "text-embedding-3-small"

    # Document chunking settings (for vector embeddings)
    document_chunk_size: int = 2048  # Characters per chunk
    document_chunk_overlap: int = 200  # Overlapping characters between chunks

    # Observability settings
    metrics_enabled: bool = True
    metrics_port: int = 9090
    otel_exporter_otlp_endpoint: Optional[str] = None
    otel_exporter_verify_ssl: bool = False
    otel_service_name: str = "nextcloud-mcp-server"
    otel_traces_sampler: str = "always_on"
    otel_traces_sampler_arg: float = 1.0
    log_format: str = "text"  # "json" or "text"
    log_level: str = "INFO"
    log_include_trace_context: bool = True

    def __post_init__(self):
        """Validate Qdrant configuration and set defaults."""
        logger = logging.getLogger(__name__)

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

        if self.document_chunk_overlap < 0:
            raise ValueError(
                f"DOCUMENT_CHUNK_OVERLAP ({self.document_chunk_overlap}) cannot be negative."
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
        import socket

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


def get_settings() -> Settings:
    """Get application settings from environment variables.

    Returns:
        Settings object with configuration values
    """
    return Settings(
        # OAuth/OIDC settings
        oidc_discovery_url=os.getenv("OIDC_DISCOVERY_URL"),
        oidc_client_id=os.getenv("NEXTCLOUD_OIDC_CLIENT_ID"),
        oidc_client_secret=os.getenv("NEXTCLOUD_OIDC_CLIENT_SECRET"),
        oidc_issuer=os.getenv("OIDC_ISSUER"),
        # Nextcloud settings
        nextcloud_host=os.getenv("NEXTCLOUD_HOST"),
        nextcloud_username=os.getenv("NEXTCLOUD_USERNAME"),
        nextcloud_password=os.getenv("NEXTCLOUD_PASSWORD"),
        # ADR-005: Token Audience Validation
        nextcloud_mcp_server_url=os.getenv("NEXTCLOUD_MCP_SERVER_URL"),
        nextcloud_resource_uri=os.getenv("NEXTCLOUD_RESOURCE_URI"),
        # Token verification endpoints
        jwks_uri=os.getenv("JWKS_URI"),
        introspection_uri=os.getenv("INTROSPECTION_URI"),
        userinfo_uri=os.getenv("USERINFO_URI"),
        # Progressive Consent settings (always enabled)
        enable_token_exchange=(
            os.getenv("ENABLE_TOKEN_EXCHANGE", "false").lower() == "true"
        ),
        enable_offline_access=(
            os.getenv("ENABLE_OFFLINE_ACCESS", "false").lower() == "true"
        ),
        # Token exchange cache settings
        token_exchange_cache_ttl=int(os.getenv("TOKEN_EXCHANGE_CACHE_TTL", "300")),
        # Token and webhook storage settings (encryption key optional for webhook-only usage)
        token_encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY"),
        token_storage_db=os.getenv("TOKEN_STORAGE_DB", "/tmp/tokens.db"),
        # Vector sync settings (ADR-007)
        vector_sync_enabled=(
            os.getenv("VECTOR_SYNC_ENABLED", "false").lower() == "true"
        ),
        vector_sync_scan_interval=int(os.getenv("VECTOR_SYNC_SCAN_INTERVAL", "300")),
        vector_sync_processor_workers=int(
            os.getenv("VECTOR_SYNC_PROCESSOR_WORKERS", "3")
        ),
        vector_sync_queue_max_size=int(
            os.getenv("VECTOR_SYNC_QUEUE_MAX_SIZE", "10000")
        ),
        # Qdrant settings
        qdrant_url=os.getenv("QDRANT_URL"),
        qdrant_location=os.getenv("QDRANT_LOCATION"),
        qdrant_api_key=os.getenv("QDRANT_API_KEY"),
        qdrant_collection=os.getenv("QDRANT_COLLECTION", "nextcloud_content"),
        # Ollama settings
        ollama_base_url=os.getenv("OLLAMA_BASE_URL"),
        ollama_embedding_model=os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"),
        ollama_verify_ssl=os.getenv("OLLAMA_VERIFY_SSL", "true").lower() == "true",
        # OpenAI settings
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_base_url=os.getenv("OPENAI_BASE_URL"),
        openai_embedding_model=os.getenv(
            "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"
        ),
        # Document chunking settings
        document_chunk_size=int(os.getenv("DOCUMENT_CHUNK_SIZE", "2048")),
        document_chunk_overlap=int(os.getenv("DOCUMENT_CHUNK_OVERLAP", "200")),
        # Observability settings
        metrics_enabled=os.getenv("METRICS_ENABLED", "true").lower() == "true",
        metrics_port=int(os.getenv("METRICS_PORT", "9090")),
        otel_exporter_otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        otel_exporter_verify_ssl=os.getenv("OTEL_EXPORTER_VERIFY_SSL", "false").lower()
        == "true",
        otel_service_name=os.getenv("OTEL_SERVICE_NAME", "nextcloud-mcp-server"),
        otel_traces_sampler=os.getenv("OTEL_TRACES_SAMPLER", "always_on"),
        otel_traces_sampler_arg=float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "1.0")),
        log_format=os.getenv("LOG_FORMAT", "text"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_include_trace_context=os.getenv("LOG_INCLUDE_TRACE_CONTEXT", "true").lower()
        == "true",
    )
