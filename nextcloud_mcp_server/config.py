import logging
import logging.config
import os
from dataclasses import dataclass
from typing import Any, Optional

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

    # Token settings
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
            logger.info("Using default Qdrant mode: in-memory (:memory:)")

        # Warn if API key set in local mode
        if self.qdrant_location and self.qdrant_api_key:
            logger.warning(
                "QDRANT_API_KEY is set but QDRANT_LOCATION is used (local mode). "
                "API key is only relevant for network mode and will be ignored."
            )


def get_settings() -> Settings:
    """Get application settings from environment variables.

    Returns:
        Settings object with configuration values
    """
    return Settings(
        # OAuth/OIDC settings
        oidc_discovery_url=os.getenv("OIDC_DISCOVERY_URL"),
        oidc_client_id=os.getenv("OIDC_CLIENT_ID"),
        oidc_client_secret=os.getenv("OIDC_CLIENT_SECRET"),
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
        # Token settings
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
    )
