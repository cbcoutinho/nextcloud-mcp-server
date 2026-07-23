"""
OpenTelemetry distributed tracing for the Nextcloud MCP Server.

This module provides:
- OpenTelemetry SDK initialization with OTLP exporter
- Auto-instrumentation for ASGI (Starlette/FastAPI) and httpx
- Helper functions for creating custom spans
- Context propagation utilities
- Span attribute standardization
"""

import logging
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

from importlib_metadata import version
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)

# Global tracer instance (initialized in setup_tracing)
_tracer: Tracer | None = None

# Auto-instrument httpx for Nextcloud API calls


def setup_tracing(
    service_name: str = "nextcloud-mcp-server",
    otlp_endpoint: str | None = None,
    otlp_verify_ssl: bool | None = None,
    sampling_rate: float = 1.0,
) -> Tracer:
    """
    Initialize OpenTelemetry tracing with OTLP exporter.

    Args:
        service_name: Service name for traces (default: "nextcloud-mcp-server")
        otlp_endpoint: OTLP gRPC endpoint (e.g., "https://collector:4317").
                      If None, tracing is initialized but no exporter is configured
        otlp_verify_ssl: Force the transport instead of deriving it from the
                      endpoint. None (default) defers to the exporter, which
                      follows the OTel spec: ``https://`` is secure, ``http://``
                      is insecure. True forces TLS, False forces plaintext —
                      needed only for a scheme-less endpoint, or plaintext
                      behind a TLS-terminating sidecar.
        sampling_rate: Sampling rate (0.0-1.0). Default 1.0 (100% sampling)

    Returns:
        Tracer instance for creating custom spans
    """
    global _tracer

    # Create resource with service name
    pkg_name = __package__.split(".")[0] if __package__ else "nextcloud_mcp_server"
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": version(pkg_name),
        }
    )

    # Create tracer provider
    provider = TracerProvider(resource=resource)

    # Configure OTLP exporter if endpoint is provided
    if otlp_endpoint:
        try:
            # Passing insecure=None hands the decision to the exporter, which
            # implements the spec (`insecure = parsed_url.scheme == "http"`)
            # and also honours OTEL_EXPORTER_OTLP_INSECURE. Passing a bool
            # unconditionally — as this did — overrides that, so an https://
            # endpoint was still dialled in plaintext and every export failed
            # with StatusCode.UNAVAILABLE against a TLS collector.
            insecure = None if otlp_verify_ssl is None else not otlp_verify_ssl
            otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=insecure)
            span_processor = BatchSpanProcessor(otlp_exporter)
            provider.add_span_processor(span_processor)
            logger.info(
                "OpenTelemetry tracing enabled with OTLP endpoint: %s", otlp_endpoint
            )
        except Exception as e:
            logger.warning(
                "Failed to initialize OTLP exporter: %s. Continuing without trace export.",
                e,
            )
    else:
        logger.info(
            "OpenTelemetry tracing initialized without OTLP exporter (traces will be generated but not exported)"
        )

    # Set global tracer provider
    trace.set_tracer_provider(provider)

    # Auto-instrument logging to inject trace context
    LoggingInstrumentor().instrument(set_logging_format=True)

    # Get and store tracer
    _tracer = trace.get_tracer(__name__)

    logger.info("OpenTelemetry tracing initialized for service: %s", service_name)
    return _tracer


def get_tracer() -> Tracer | None:
    """
    Get the global tracer instance.

    Returns:
        Tracer instance for creating custom spans, or None if tracing is not enabled

    Note:
        Returns None if setup_tracing() was never called (tracing disabled).
        Calling code should handle None gracefully.
    """
    return _tracer


@contextmanager
def trace_server_request(
    operation_name: str,
    carrier: Mapping[str, str],
    attributes: dict[str, Any] | None = None,
):
    """Start a SERVER span parented to the caller's inbound trace context.

    Without this every request started a brand-new trace: nothing linked an
    Astrolabe request to the work this server did for it, and traces showed up
    with ``<root span not yet received>``. Astrolabe runs on a separate host and
    only reaches us over HTTP, so W3C ``traceparent`` on the request is the only
    way the two halves can be stitched together.

    Uses the globally configured propagator rather than instantiating
    ``TraceContextTextMapPropagator`` directly, so a deployment that configures
    additional propagators (e.g. baggage) keeps working. An absent or malformed
    header yields an empty context, which starts a fresh root span — the
    previous behaviour, so an uninstrumented caller degrades rather than breaks.

    Args:
        operation_name: Span name.
        carrier: Inbound request headers to extract trace context from.
        attributes: Optional attributes to set on the span.

    Yields:
        The span, or None when tracing is disabled.
    """
    tracer = get_tracer()

    if tracer is None:
        yield None
        return

    parent_context = extract(carrier)

    with tracer.start_as_current_span(
        operation_name, context=parent_context, kind=SpanKind.SERVER
    ) as span:
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, value)

        try:
            yield span
            # Only claim success if the body did not already record a failure.
            # The middleware marks 5xx responses as errors from inside this
            # block — a handler that catches its own exception and returns a
            # 500 never raises here — and an unconditional OK would overwrite
            # that, leaving the failure invisible to `status=error` queries.
            if span.status.status_code is StatusCode.UNSET:
                span.set_status(Status(StatusCode.OK))
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise


@contextmanager
def trace_operation(
    operation_name: str,
    attributes: dict[str, Any] | None = None,
    record_exception: bool = True,
):
    """
    Context manager for tracing an operation with automatic error handling.

    Usage:
        with trace_operation("mcp.tool.nc_notes_create_note", {"note.title": "My Note"}):
            # Your code here
            pass

    Args:
        operation_name: Name of the operation (span name)
        attributes: Optional attributes to add to the span
        record_exception: Whether to record exceptions in the span (default: True)

    Yields:
        Span instance for adding additional attributes (or None if tracing disabled)
    """
    tracer = get_tracer()

    # If tracing is not enabled, just yield without creating a span
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(operation_name) as span:
        # Set initial attributes
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, value)

        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            if record_exception:
                span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise


def trace_mcp_tool(tool_name: str, tool_args: dict[str, Any] | None = None):
    """
    Create a span for an MCP tool invocation.

    Usage:
        with trace_mcp_tool("nc_notes_create_note", {"title": "My Note"}):
            # Tool implementation
            pass

    Args:
        tool_name: Name of the MCP tool
        tool_args: Optional tool arguments (sensitive data will be sanitized)

    Returns:
        Context manager for the span
    """
    attributes = {
        "mcp.tool.name": tool_name,
    }

    # Add sanitized tool args (avoid logging sensitive data)
    if tool_args:
        # Only include non-sensitive arguments
        safe_args = {
            k: v
            for k, v in tool_args.items()
            if k not in ("password", "token", "secret", "api_key", "etag")
        }
        if safe_args:
            attributes["mcp.tool.args"] = str(safe_args)

    return trace_operation(f"mcp.tool.{tool_name}", attributes)


def trace_nextcloud_api_call(
    app: str,
    method: str,
    path: str | None = None,
):
    """
    Create a span for a Nextcloud API call.

    Usage:
        with trace_nextcloud_api_call("notes", "POST", "/apps/notes/api/v1/notes"):
            # API call implementation
            pass

    Args:
        app: Nextcloud app name (notes, calendar, contacts, etc.)
        method: HTTP method (GET, POST, PUT, DELETE, etc.)
        path: Optional API path

    Returns:
        Context manager for the span
    """
    attributes = {
        "nextcloud.app": app,
        "http.method": method,
    }

    if path:
        attributes["http.path"] = path

    return trace_operation(f"nextcloud.api.{app}.{method}", attributes)


def trace_oauth_operation(operation: str, details: dict[str, Any] | None = None):
    """
    Create a span for an OAuth operation.

    Usage:
        with trace_oauth_operation("token.validate", {"method": "jwt"}):
            # OAuth validation logic
            pass

    Args:
        operation: OAuth operation name (e.g., "token.validate", "token.refresh")
        details: Optional operation details (sensitive data will be sanitized)

    Returns:
        Context manager for the span
    """
    attributes = {"oauth.operation": operation}

    if details:
        # Only include non-sensitive details
        safe_details = {
            k: v
            for k, v in details.items()
            if k not in ("token", "refresh_token", "access_token", "client_secret")
        }
        if safe_details:
            attributes.update(safe_details)

    return trace_operation(f"oauth.{operation}", attributes)


def trace_vector_sync_operation(
    operation: str,
    document_count: int | None = None,
):
    """
    Create a span for a vector sync operation.

    Usage:
        with trace_vector_sync_operation("scan", document_count=10):
            # Vector sync logic
            pass

    Args:
        operation: Operation name (scan, process, embed, upsert)
        document_count: Optional number of documents being processed

    Returns:
        Context manager for the span
    """
    attributes = {"vector_sync.operation": operation}

    if document_count is not None:
        attributes["vector_sync.document_count"] = document_count

    return trace_operation(f"vector_sync.{operation}", attributes)


def trace_db_operation(
    db: str,
    operation: str,
    table: str | None = None,
):
    """
    Create a span for a database operation.

    Usage:
        with trace_db_operation("sqlite", "insert", "refresh_tokens"):
            # Database operation
            pass

    Args:
        db: Database type (sqlite, postgresql, qdrant)
        operation: Operation type (insert, select, update, delete, upsert, search)
        table: Optional table/collection name

    Returns:
        Context manager for the span
    """
    attributes = {
        "db.system": db,
        "db.operation": operation,
    }

    if table:
        attributes["db.table"] = table

    return trace_operation(f"db.{db}.{operation}", attributes)


def trace_db_connect(db: str):
    """
    Create a span for acquiring a database connection.

    Nests inside :func:`trace_db_operation`, splitting "how long did the query
    take" from "how long did it take to get a connection at all". Deck #678: a
    single span covering both hid a ~600ms connect inside an apparently slow
    insert, because under NullPool (ADR-026) every operation opens a fresh
    connection and pays the full TCP + TLS + auth handshake.

    Usage:
        with trace_db_connect("postgresql"):
            conn = await engine.connect()

    Args:
        db: Database type (sqlite, postgresql)

    Returns:
        Context manager for the span
    """
    return trace_operation(f"db.{db}.connect", {"db.system": db})


def add_span_attribute(key: str, value: Any) -> None:
    """
    Add an attribute to the current span (if any).

    Args:
        key: Attribute key
        value: Attribute value

    Note:
        This is a no-op if tracing is not enabled or there's no active span.
    """
    if _tracer is None:
        return  # Tracing not enabled
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(key, value)


def add_span_event(name: str, attributes: dict[str, Any] | None = None) -> None:
    """
    Add an event to the current span (if any).

    Args:
        name: Event name
        attributes: Optional event attributes

    Note:
        This is a no-op if tracing is not enabled or there's no active span.
    """
    if _tracer is None:
        return  # Tracing not enabled
    span = trace.get_current_span()
    if span.is_recording():
        span.add_event(name, attributes=attributes or {})


def get_trace_context() -> dict[str, str]:
    """
    Get current trace context as a dictionary.

    Returns:
        Dictionary with trace_id and span_id (or empty dict if tracing disabled or no active span)
    """
    if _tracer is None:
        return {}  # Tracing not enabled

    span = trace.get_current_span()
    if span.is_recording():
        span_context = span.get_span_context()
        return {
            "trace_id": format(span_context.trace_id, "032x"),
            "span_id": format(span_context.span_id, "016x"),
        }
    return {}
