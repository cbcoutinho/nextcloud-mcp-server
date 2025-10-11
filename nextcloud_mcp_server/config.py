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


def get_unstructured_strategy() -> str:
    """Get the parsing strategy for the Unstructured API.

    Valid values are:
    - 'auto': Automatically choose the best strategy (default)
    - 'fast': Fast parsing without OCR
    - 'hi_res': High-resolution parsing with OCR for better accuracy

    Returns:
        The parsing strategy to use.
    """
    strategy = os.getenv("UNSTRUCTURED_STRATEGY", "auto").lower()
    valid_strategies = ["auto", "fast", "hi_res"]

    if strategy not in valid_strategies:
        logging.warning(
            f"Invalid UNSTRUCTURED_STRATEGY '{strategy}'. Using 'hi_res'. "
            f"Valid options: {', '.join(valid_strategies)}"
        )
        return "hi_res"

    return strategy


def get_unstructured_languages() -> list[str]:
    """Get the OCR languages for the Unstructured API.

    Languages should be specified as ISO 639-3 codes (e.g., 'eng', 'deu', 'fra').
    Multiple languages can be specified separated by commas.

    Default languages: English (eng) and German (deu)

    Common language codes:
    - eng: English
    - deu: German
    - fra: French
    - spa: Spanish
    - ita: Italian
    - por: Portuguese
    - rus: Russian
    - ara: Arabic
    - zho: Chinese
    - jpn: Japanese
    - kor: Korean

    Returns:
        List of language codes for OCR processing.
    """
    languages_str = os.getenv("UNSTRUCTURED_LANGUAGES", "eng,deu")

    # Split by comma and clean up whitespace
    languages = [lang.strip() for lang in languages_str.split(",") if lang.strip()]

    if not languages:
        logging.warning(
            "No languages specified in UNSTRUCTURED_LANGUAGES. Using default: eng,deu"
        )
        return ["eng", "deu"]

    return languages
