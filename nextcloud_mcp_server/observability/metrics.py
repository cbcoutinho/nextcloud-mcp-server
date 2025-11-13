"""
Prometheus metrics for the Nextcloud MCP Server.

This module defines all Prometheus metrics for monitoring server health, performance,
and resource usage. Metrics are organized by category:

- HTTP Server Metrics (RED: Rate, Errors, Duration)
- MCP Tool Metrics (per-tool invocation tracking)
- MCP Resource Metrics
- Nextcloud API Client Metrics
- OAuth Flow Metrics
- Vector Sync Metrics (conditional on feature flag)
- Database Operation Metrics
- External Dependency Health Metrics
"""

import logging

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

logger = logging.getLogger(__name__)

# =============================================================================
# HTTP Server Metrics (RED + System)
# =============================================================================

http_requests_total = Counter(
    "mcp_http_requests_total",
    "Total HTTP requests received",
    ["method", "endpoint", "status_code"],
)

http_request_duration_seconds = Histogram(
    "mcp_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

http_requests_in_progress = Gauge(
    "mcp_http_requests_in_progress",
    "Number of HTTP requests currently being processed",
    ["method", "endpoint"],
)

# =============================================================================
# MCP Tool Metrics
# =============================================================================

mcp_tool_calls_total = Counter(
    "mcp_tool_calls_total",
    "Total MCP tool invocations",
    ["tool_name", "status"],  # status: success | error
)

mcp_tool_duration_seconds = Histogram(
    "mcp_tool_duration_seconds",
    "MCP tool execution duration in seconds",
    ["tool_name"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

mcp_tool_errors_total = Counter(
    "mcp_tool_errors_total",
    "Total MCP tool errors by type",
    ["tool_name", "error_type"],
)

# =============================================================================
# MCP Resource Metrics
# =============================================================================

mcp_resource_requests_total = Counter(
    "mcp_resource_requests_total",
    "Total MCP resource requests",
    ["resource_uri", "status"],
)

mcp_resource_duration_seconds = Histogram(
    "mcp_resource_duration_seconds",
    "MCP resource request duration in seconds",
    ["resource_uri"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# =============================================================================
# Nextcloud API Client Metrics
# =============================================================================

nextcloud_api_requests_total = Counter(
    "mcp_nextcloud_api_requests_total",
    "Total Nextcloud API requests",
    ["app", "method", "status_code"],  # app: notes, calendar, contacts, etc.
)

nextcloud_api_duration_seconds = Histogram(
    "mcp_nextcloud_api_duration_seconds",
    "Nextcloud API request duration in seconds",
    ["app", "method"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

nextcloud_api_retries_total = Counter(
    "mcp_nextcloud_api_retries_total",
    "Total Nextcloud API retries",
    ["app", "reason"],  # reason: 429 | timeout | connection_error
)

# =============================================================================
# OAuth Flow Metrics
# =============================================================================

oauth_token_validations_total = Counter(
    "mcp_oauth_token_validations_total",
    "Total OAuth token validation attempts",
    ["method", "result"],  # method: introspect | jwt; result: valid | invalid | error
)

oauth_token_exchange_total = Counter(
    "mcp_oauth_token_exchange_total",
    "Total OAuth token exchange operations (RFC 8693)",
    ["status"],  # status: success | error
)

oauth_token_cache_hits_total = Counter(
    "mcp_oauth_token_cache_hits_total",
    "Total OAuth token cache lookups",
    ["hit"],  # hit: true | false
)

oauth_refresh_token_operations_total = Counter(
    "mcp_oauth_refresh_token_operations_total",
    "Total refresh token storage operations",
    [
        "operation",
        "status",
    ],  # operation: store | retrieve | delete; status: success | error
)

# =============================================================================
# Vector Sync Metrics (optional feature)
# =============================================================================

vector_sync_documents_scanned_total = Counter(
    "mcp_vector_sync_documents_scanned_total",
    "Total documents scanned for vector sync",
)

vector_sync_documents_processed_total = Counter(
    "mcp_vector_sync_documents_processed_total",
    "Total documents processed for vector sync",
    ["status"],  # status: success | error
)

vector_sync_processing_duration_seconds = Histogram(
    "mcp_vector_sync_processing_duration_seconds",
    "Document processing duration in seconds",
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

vector_sync_queue_size = Gauge(
    "mcp_vector_sync_queue_size",
    "Current number of documents in processing queue",
)

qdrant_operations_total = Counter(
    "mcp_qdrant_operations_total",
    "Total Qdrant vector database operations",
    [
        "operation",
        "status",
    ],  # operation: upsert | search | delete; status: success | error
)

# =============================================================================
# Database Metrics
# =============================================================================

db_operations_total = Counter(
    "mcp_db_operations_total",
    "Total database operations",
    ["db", "operation", "status"],  # db: sqlite | qdrant; operation varies
)

db_operation_duration_seconds = Histogram(
    "mcp_db_operation_duration_seconds",
    "Database operation duration in seconds",
    ["db", "operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# =============================================================================
# External Dependency Health Metrics
# =============================================================================

dependency_health = Gauge(
    "mcp_dependency_health",
    "External dependency health status (1=up, 0=down)",
    ["dependency"],  # dependency: nextcloud | keycloak | qdrant | unstructured
)

dependency_check_duration_seconds = Histogram(
    "mcp_dependency_check_duration_seconds",
    "Dependency health check duration in seconds",
    ["dependency"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# =============================================================================
# Metrics Setup and HTTP Handler
# =============================================================================


def setup_metrics(port: int = 9090) -> None:
    """
    Initialize Prometheus metrics collection and start HTTP server.

    Starts a dedicated HTTP server on the specified port to serve metrics.
    This server runs in a separate thread and is isolated from the main application.

    Args:
        port: Port to serve metrics on (default: 9090)

    Note:
        Metrics endpoint (/metrics) is ONLY accessible on this dedicated port,
        not on the main application HTTP port. This is a security best practice
        to prevent external exposure of metrics.
    """
    try:
        start_http_server(port)
        logger.info(f"Prometheus metrics server started on port {port}")
    except OSError as e:
        if "Address already in use" in str(e):
            logger.warning(
                f"Metrics port {port} already in use (metrics server likely already running)"
            )
        else:
            logger.error(f"Failed to start metrics server on port {port}: {e}")
            raise


# =============================================================================
# Convenience Functions for Common Metric Updates
# =============================================================================


def record_tool_call(tool_name: str, duration: float, status: str = "success") -> None:
    """
    Record metrics for an MCP tool call.

    Args:
        tool_name: Name of the MCP tool
        duration: Execution duration in seconds
        status: "success" or "error"
    """
    mcp_tool_calls_total.labels(tool_name=tool_name, status=status).inc()
    mcp_tool_duration_seconds.labels(tool_name=tool_name).observe(duration)


def record_tool_error(tool_name: str, error_type: str) -> None:
    """
    Record an MCP tool error.

    Args:
        tool_name: Name of the MCP tool
        error_type: Type of error (e.g., "HTTPStatusError", "ValueError")
    """
    mcp_tool_errors_total.labels(tool_name=tool_name, error_type=error_type).inc()


def record_nextcloud_api_call(
    app: str,
    method: str,
    status_code: int,
    duration: float,
) -> None:
    """
    Record metrics for a Nextcloud API call.

    Args:
        app: Nextcloud app name (notes, calendar, contacts, etc.)
        method: HTTP method (GET, POST, PUT, DELETE, PROPFIND, etc.)
        status_code: HTTP status code
        duration: Request duration in seconds
    """
    nextcloud_api_requests_total.labels(
        app=app, method=method, status_code=str(status_code)
    ).inc()
    nextcloud_api_duration_seconds.labels(app=app, method=method).observe(duration)


def record_nextcloud_api_retry(app: str, reason: str) -> None:
    """
    Record a Nextcloud API retry.

    Args:
        app: Nextcloud app name
        reason: Retry reason (429, timeout, connection_error)
    """
    nextcloud_api_retries_total.labels(app=app, reason=reason).inc()


def record_oauth_token_validation(method: str, result: str) -> None:
    """
    Record an OAuth token validation.

    Args:
        method: Validation method ("introspect" or "jwt")
        result: Validation result ("valid", "invalid", or "error")
    """
    oauth_token_validations_total.labels(method=method, result=result).inc()


def record_db_operation(
    db: str, operation: str, duration: float, status: str = "success"
) -> None:
    """
    Record a database operation.

    Args:
        db: Database type ("sqlite" or "qdrant")
        operation: Operation type (e.g., "insert", "select", "upsert", "search")
        duration: Operation duration in seconds
        status: "success" or "error"
    """
    db_operations_total.labels(db=db, operation=operation, status=status).inc()
    db_operation_duration_seconds.labels(db=db, operation=operation).observe(duration)


def set_dependency_health(dependency: str, is_healthy: bool) -> None:
    """
    Update external dependency health status.

    Args:
        dependency: Dependency name (nextcloud, keycloak, qdrant, unstructured)
        is_healthy: True if dependency is healthy, False otherwise
    """
    dependency_health.labels(dependency=dependency).set(1 if is_healthy else 0)


def record_dependency_check(dependency: str, duration: float) -> None:
    """
    Record a dependency health check duration.

    Args:
        dependency: Dependency name
        duration: Check duration in seconds
    """
    dependency_check_duration_seconds.labels(dependency=dependency).observe(duration)


def record_vector_sync_scan(documents_found: int) -> None:
    """
    Record documents scanned during vector sync.

    Args:
        documents_found: Number of documents discovered in scan
    """
    vector_sync_documents_scanned_total.inc(documents_found)


def record_vector_sync_processing(duration: float, status: str = "success") -> None:
    """
    Record document processing with duration and status.

    Args:
        duration: Processing duration in seconds
        status: "success" or "error"
    """
    vector_sync_documents_processed_total.labels(status=status).inc()
    vector_sync_processing_duration_seconds.observe(duration)


def record_qdrant_operation(operation: str, status: str = "success") -> None:
    """
    Record Qdrant vector database operation.

    Args:
        operation: Operation type ("upsert", "search", "delete")
        status: "success" or "error"
    """
    qdrant_operations_total.labels(operation=operation, status=status).inc()


def update_vector_sync_queue_size(size: int) -> None:
    """
    Update vector sync queue size gauge.

    Args:
        size: Current queue size
    """
    vector_sync_queue_size.set(size)
