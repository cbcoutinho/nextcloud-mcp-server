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

    # Nextcloud settings
    nextcloud_host: Optional[str] = None
    nextcloud_username: Optional[str] = None
    nextcloud_password: Optional[str] = None

    # Progressive Consent settings (always enabled - no flag needed)
    enable_token_exchange: bool = False
    enable_offline_access: bool = False

    # Token settings
    token_encryption_key: Optional[str] = None
    token_storage_db: Optional[str] = None


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
        # Nextcloud settings
        nextcloud_host=os.getenv("NEXTCLOUD_HOST"),
        nextcloud_username=os.getenv("NEXTCLOUD_USERNAME"),
        nextcloud_password=os.getenv("NEXTCLOUD_PASSWORD"),
        # Progressive Consent settings (always enabled)
        enable_token_exchange=(
            os.getenv("ENABLE_TOKEN_EXCHANGE", "false").lower() == "true"
        ),
        enable_offline_access=(
            os.getenv("ENABLE_OFFLINE_ACCESS", "false").lower() == "true"
        ),
        # Token settings
        token_encryption_key=os.getenv("TOKEN_ENCRYPTION_KEY"),
        token_storage_db=os.getenv("TOKEN_STORAGE_DB", "/tmp/tokens.db"),
    )
