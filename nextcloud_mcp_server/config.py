import logging.config
import os
from typing import Optional

LOGGING_CONFIG = {
    "version": 1,
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "http",
        }
    },
    "formatters": {
        "http": {
            "format": "%(levelname)s [%(asctime)s] %(name)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        }
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
    },
}


def setup_logging():
    logging.config.dictConfig(LOGGING_CONFIG)


# Document Parsing Configuration
def get_unstructured_api_url() -> Optional[str]:
    """Get the Unstructured API URL from environment variables.
    
    Returns:
        The Unstructured API URL if parsing is enabled, None otherwise.
    """
    enabled = os.getenv("ENABLE_UNSTRUCTURED_PARSING", "true").lower() == "true"
    if not enabled:
        return None
    
    return os.getenv("UNSTRUCTURED_API_URL", "http://unstructured:8000")


def is_unstructured_parsing_enabled() -> bool:
    """Check if unstructured document parsing is enabled.
    
    Returns:
        True if enabled, False otherwise.
    """
    return os.getenv("ENABLE_UNSTRUCTURED_PARSING", "true").lower() == "true"
