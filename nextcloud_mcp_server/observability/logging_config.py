"""
Enhanced logging configuration for the Nextcloud MCP Server.

This module provides:
- Structured JSON logging with python-json-logger
- Trace context injection (trace_id, span_id) for correlation with distributed traces
- Configurable log formats (JSON or text)
- Log level configuration per component
"""

import logging
import sys
from collections.abc import Iterator
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from nextcloud_mcp_server.observability.tracing import get_trace_context

# Exception types whose traceback is noise on the noisy library loggers that
# re-log every propagated exception with ``exc_info`` (``procrastinate.worker``
# in particular). They are either control-flow signals our pipeline raises on
# purpose (tier escalation, batch-pending) or expected transient/handled
# infrastructure failures (network blips to Qdrant / Nextcloud / the
# embedding-gateway, a deduped duplicate job). For these the multi-frame
# traceback adds nothing over the one-line message, and there are thousands a
# day in production. Matched by class name so optional deps (psycopg, qdrant)
# needn't be importable. Genuinely unexpected exceptions are NOT listed here, so
# they keep their traceback and real bugs stay debuggable.
_EXPECTED_EXCEPTION_NAMES: frozenset[str] = frozenset(
    {
        # Tier-escalation control-flow signals (document_processors.escalation).
        "EscalateError",
        "BatchPending",
        # Transient HTTP failures to Qdrant / Nextcloud / embedding-gateway.
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "HTTPStatusError",
        # qdrant-client wraps transport errors in this.
        "ResponseHandlingException",
        # procrastinate queueing_lock dedup (psycopg): a live duplicate job.
        "UniqueViolation",
    }
)


def _iter_leaf_exceptions(exc: BaseException) -> Iterator[BaseException]:
    """Yield the leaf exceptions of ``exc``, descending ``BaseExceptionGroup``s.

    Vector-sync work runs inside anyio task groups, so a failure can surface as
    a (possibly nested) ``BaseExceptionGroup``; flatten it so the filter can
    inspect the real causes.
    """
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _iter_leaf_exceptions(sub)
    else:
        yield exc


class ExpectedExceptionFilter(logging.Filter):
    """Strip the traceback from records whose exception is expected/handled.

    Attached to library loggers we cannot edit at the call site (notably
    ``procrastinate.worker``, which re-logs every propagated task exception with
    ``exc_info`` — including our tier-escalation control-flow signals and
    transient network blips — as a full multi-frame traceback). The record is
    always kept; only ``exc_info`` is cleared so the formatter emits the
    one-line message without the traceback. Records whose exception (or any leaf
    of its exception group) is NOT in ``_EXPECTED_EXCEPTION_NAMES`` are left
    untouched, so genuinely unexpected exceptions keep their traceback.
    """

    # This is a mutation-only filter: it always keeps the record (returns True)
    # and only ever clears exc_info. The invariant return is intentional — a
    # logging.Filter that never drops a record is a standard pattern — so the
    # bare ``# NOSONAR`` silences python:S3516 (invariant return value).
    def filter(self, record: logging.LogRecord) -> bool:  # NOSONAR
        exc_info = record.exc_info
        if not exc_info or exc_info[1] is None:
            return True
        leaves = list(_iter_leaf_exceptions(exc_info[1]))
        if leaves and all(
            type(leaf).__name__ in _EXPECTED_EXCEPTION_NAMES for leaf in leaves
        ):
            # Expected/handled condition: drop the traceback, keep the message.
            record.exc_info = None
            record.exc_text = None
        return True


# Library loggers that re-log expected/handled exceptions with ``exc_info``.
# The filter is type-gated, so listing a logger here only suppresses tracebacks
# for the expected exception types above — never a genuine unhandled bug.
#
# - ``procrastinate.worker``: re-logs every propagated task exception (tier
#   escalation, transient HTTP) as a full traceback, even at INFO ("to retry").
# - ``asyncio``: the event loop's default handler logs "Task exception was never
#   retrieved" with a traceback when procrastinate's internal escalation re-defer
#   trips the ``queueing_lock`` partial-unique index (a benign live-duplicate
#   dedup). Type-gating means a real unhandled-task bug still keeps its traceback.
_EXPECTED_EXCEPTION_LOGGERS: tuple[str, ...] = ("procrastinate.worker", "asyncio")


def install_expected_exception_filter() -> None:
    """Attach :class:`ExpectedExceptionFilter` to the noisy library loggers.

    Used by the imperative :func:`setup_logging` path (the standalone ingest
    worker); the uvicorn ``dictConfig`` path wires the same filter declaratively.
    """
    exc_filter = ExpectedExceptionFilter()
    for name in _EXPECTED_EXCEPTION_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, ExpectedExceptionFilter) for f in logger.filters):
            logger.addFilter(exc_filter)


class HealthCheckFilter(logging.Filter):
    """
    Logging filter that excludes health check endpoint requests.

    This prevents health check polls from cluttering logs while keeping
    access logs for all other endpoints.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Filter out health check requests from uvicorn access logs.

        Args:
            record: LogRecord instance

        Returns:
            False if this is a health check request, True otherwise
        """
        # Check if the log message contains health check endpoints
        message = record.getMessage()
        health_check = any(
            endpoint in message
            for endpoint in [
                "/health/live",
                "/health/ready",
                "/metrics",
                "/app/vector-sync/status",
            ]
        )

        return not health_check


class TraceContextFormatter(JsonFormatter):
    """
    JSON formatter that injects OpenTelemetry trace context into log records.

    This allows logs to be correlated with distributed traces by including
    trace_id and span_id in each log entry.
    """

    def add_fields(
        self,
        log_data: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        """
        Add custom fields to the log record, including trace context.

        Args:
            log_data: Dictionary to be serialized as JSON
            record: LogRecord instance
            message_dict: Dictionary of extra fields from log call
        """
        # Call parent to add standard fields
        super().add_fields(log_data, record, message_dict)

        # Add trace context if available
        trace_context = get_trace_context()
        if trace_context:
            log_data["trace_id"] = trace_context.get("trace_id")
            log_data["span_id"] = trace_context.get("span_id")

        # Add standard fields with consistent naming
        log_data["timestamp"] = self.formatTime(record)
        log_data["level"] = record.levelname
        log_data["logger"] = record.name
        log_data["message"] = record.getMessage()

        # Include exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)


class TraceContextTextFormatter(logging.Formatter):
    """
    Text formatter that includes OpenTelemetry trace context.

    Format: [LEVEL] [timestamp] logger - message [trace_id=xxx span_id=yyy]
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Format log record with trace context.

        Args:
            record: LogRecord instance

        Returns:
            Formatted log string
        """
        # Format base message
        base_message = super().format(record)

        # Add trace context if available
        trace_context = get_trace_context()
        if trace_context:
            trace_id = trace_context.get("trace_id", "")
            span_id = trace_context.get("span_id", "")
            return f"{base_message} [trace_id={trace_id} span_id={span_id}]"

        return base_message


def setup_logging(
    log_format: str = "json",
    log_level: str = "INFO",
    include_trace_context: bool = True,
) -> None:
    """
    Configure logging for the Nextcloud MCP Server.

    Args:
        log_format: "json" for JSON logging, "text" for human-readable text (default: "json")
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL) (default: "INFO")
        include_trace_context: Whether to include trace context in logs (default: True)
    """
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove existing handlers
    root_logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Configure formatter based on format preference
    if log_format.lower() == "json":
        if include_trace_context:
            formatter = TraceContextFormatter(
                "%(timestamp)s %(level)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        else:
            formatter = JsonFormatter(
                "%(timestamp)s %(level)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
    else:  # text format
        if include_trace_context:
            formatter = TraceContextTextFormatter(
                "%(levelname)s [%(asctime)s] %(name)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        else:
            formatter = logging.Formatter(
                "%(levelname)s [%(asctime)s] %(name)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Configure specific logger levels
    configure_component_loggers(log_level)

    # Suppress tracebacks for expected/handled exceptions re-logged by noisy
    # library loggers (e.g. procrastinate.worker). This is the worker path; the
    # uvicorn dictConfig wires the same filter declaratively.
    install_expected_exception_filter()

    root_logger.info(
        "Logging configured: format=%s, level=%s, trace_context=%s",
        log_format,
        log_level,
        include_trace_context,
    )


def configure_component_loggers(default_level: str = "INFO") -> None:
    """
    Configure log levels for specific components.

    This allows fine-grained control over logging verbosity for different
    parts of the application.

    Args:
        default_level: Default log level for most components
    """
    # Map of logger names to log levels
    logger_levels = {
        # Application loggers
        "nextcloud_mcp_server": default_level,
        "nextcloud_mcp_server.server": default_level,
        "nextcloud_mcp_server.client": default_level,
        "nextcloud_mcp_server.auth": default_level,
        "nextcloud_mcp_server.observability": default_level,
        # HTTP client loggers (less verbose by default)
        "httpx": "WARNING",
        "httpcore": "WARNING",
        # Server loggers
        "uvicorn": "INFO",
        "uvicorn.access": "INFO",
        "uvicorn.error": "INFO",
        # MCP framework
        "mcp": "INFO",
        # OpenTelemetry (less verbose)
        "opentelemetry": "WARNING",
    }

    for logger_name, level in logger_levels.items():
        logger = logging.getLogger(logger_name)
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a specific module.

    This is a convenience function that wraps logging.getLogger()
    to ensure consistent logger configuration.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


def get_uvicorn_logging_config(
    log_format: str = "json",
    log_level: str = "INFO",
    include_trace_context: bool = True,
) -> dict:
    """
    Get uvicorn-compatible logging configuration.

    This creates a logging config dict that uvicorn can use while maintaining
    our observability setup (JSON format, trace context, etc.).

    Args:
        log_format: "json" or "text"
        log_level: Minimum log level
        include_trace_context: Whether to include trace IDs in logs

    Returns:
        Logging config dict compatible with uvicorn's log_config parameter
    """
    # Determine formatter class based on format and trace context
    if log_format.lower() == "json":
        if include_trace_context:
            formatter_class = "nextcloud_mcp_server.observability.logging_config.TraceContextFormatter"
        else:
            formatter_class = "pythonjsonlogger.json.JsonFormatter"
        format_string = "%(timestamp)s %(level)s %(name)s %(message)s"
    else:
        if include_trace_context:
            formatter_class = "nextcloud_mcp_server.observability.logging_config.TraceContextTextFormatter"
        else:
            formatter_class = "logging.Formatter"
        format_string = "%(levelname)s [%(asctime)s] %(name)s - %(message)s"

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": formatter_class,
                "format": format_string,
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "filters": {
            "health_check_filter": {
                "()": "nextcloud_mcp_server.observability.logging_config.HealthCheckFilter",
            },
            "expected_exception_filter": {
                "()": "nextcloud_mcp_server.observability.logging_config.ExpectedExceptionFilter",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "access": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "filters": ["health_check_filter"],
            },
        },
        "loggers": {
            "": {
                "handlers": ["default"],
                "level": log_level.upper(),
            },
            # procrastinate re-logs every propagated task exception (including our
            # tier-escalation control-flow signals and transient network blips)
            # with a full traceback; strip those, keeping genuine bugs' tracebacks.
            "procrastinate.worker": {
                "handlers": ["default"],
                "level": "INFO",
                "filters": ["expected_exception_filter"],
                "propagate": False,
            },
            # asyncio logs "Task exception was never retrieved" with a traceback
            # when procrastinate's escalation re-defer trips queueing_lock (benign
            # dedup); the type-gated filter strips only that, not real bugs.
            "asyncio": {
                "handlers": ["default"],
                "level": "WARNING",
                "filters": ["expected_exception_filter"],
                "propagate": False,
            },
            "uvicorn": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "httpx": {
                "handlers": ["default"],
                "level": "WARNING",
                "propagate": False,
            },
            "httpcore": {
                "handlers": ["default"],
                "level": "WARNING",
                "propagate": False,
            },
            "opentelemetry": {
                "handlers": ["default"],
                "level": "WARNING",
                "propagate": False,
            },
        },
    }
