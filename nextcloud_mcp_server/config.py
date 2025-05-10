import logging.config

LOGGING_CONFIG = {
    "version": 1,
    "handlers": {
        "default": {
            "class": "logging.FileHandler",
            "formatter": "http",
            # "stream": "ext://sys.stderr"
            "filename": "/tmp/nextcloud-mcp-server.log",
            "mode": "a",
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
            "level": "DEBUG",
        },
        "httpx": {
            "handlers": ["default"],
            "level": "DEBUG",
            "propagate": False,  # Prevent propagation to root logger
        },
        "httpcore": {
            "handlers": ["default"],
            "level": "DEBUG",
            "propagate": False,  # Prevent propagation to root logger
        },
    },
}


def setup_logging():
    logging.config.dictConfig(LOGGING_CONFIG)
