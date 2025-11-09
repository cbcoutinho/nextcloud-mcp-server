"""
Observability middleware for the Nextcloud MCP Server.

This module provides Starlette middleware that automatically instruments
HTTP requests with:
- Prometheus metrics (request count, latency, in-flight requests)
- OpenTelemetry distributed tracing
- Request/response timing and error tracking
"""

import logging
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from nextcloud_mcp_server.observability.metrics import (
    http_request_duration_seconds,
    http_requests_in_progress,
    http_requests_total,
)
from nextcloud_mcp_server.observability.tracing import (
    add_span_attribute,
    trace_operation,
)

logger = logging.getLogger(__name__)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware for automatic HTTP request instrumentation.

    This middleware:
    - Records Prometheus metrics for each request (RED metrics)
    - Creates OpenTelemetry spans for distributed tracing
    - Tracks request timing and errors
    - Handles in-flight request counting
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> Response:
        """
        Process HTTP request with observability instrumentation.

        Args:
            request: Starlette request object
            call_next: Next middleware or route handler

        Returns:
            Response from downstream handler
        """
        # Extract request details
        method = request.method
        path = request.url.path
        endpoint = self._get_endpoint_label(path)

        # Increment in-flight requests counter
        http_requests_in_progress.labels(method=method, endpoint=endpoint).inc()

        # Record start time
        start_time = time.time()

        try:
            # Create span for request (OpenTelemetry auto-instrumentation will create parent span)
            with trace_operation(
                f"HTTP {method} {endpoint}",
                attributes={
                    "http.method": method,
                    "http.path": path,
                    "http.scheme": request.url.scheme,
                    "http.host": request.url.hostname,
                },
            ):
                # Process request
                response = await call_next(request)

                # Add response status to span
                add_span_attribute("http.status_code", response.status_code)

                # Record metrics
                duration = time.time() - start_time
                self._record_request_metrics(
                    method=method,
                    endpoint=endpoint,
                    status_code=response.status_code,
                    duration=duration,
                )

                return response

        except Exception:
            # Record error metrics
            duration = time.time() - start_time
            self._record_request_metrics(
                method=method,
                endpoint=endpoint,
                status_code=500,  # Internal server error
                duration=duration,
            )

            logger.error(
                f"Request failed: {method} {path}",
                exc_info=True,
                extra={
                    "method": method,
                    "path": path,
                    "duration_seconds": duration,
                },
            )

            # Re-raise exception to be handled by error middleware
            raise

        finally:
            # Decrement in-flight requests counter
            http_requests_in_progress.labels(method=method, endpoint=endpoint).dec()

    def _get_endpoint_label(self, path: str) -> str:
        """
        Get endpoint label for metrics, normalizing dynamic path segments.

        This prevents metric cardinality explosion by grouping similar paths.

        Args:
            path: Request path

        Returns:
            Normalized endpoint label
        """
        # Health check endpoints
        if path.startswith("/health/"):
            return "/health/*"

        # Metrics endpoint
        if path == "/metrics":
            return "/metrics"

        # MCP protocol endpoints
        if path == "/sse" or path.startswith("/sse/"):
            return "/sse"

        if path == "/messages" or path.startswith("/messages/"):
            return "/messages"

        # OAuth/OIDC endpoints
        if path.startswith("/oauth/"):
            return "/oauth/*"

        if path.startswith("/oidc/"):
            return "/oidc/*"

        # Catch-all for other paths
        return path

    def _record_request_metrics(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        duration: float,
    ) -> None:
        """
        Record Prometheus metrics for an HTTP request.

        Args:
            method: HTTP method
            endpoint: Normalized endpoint label
            status_code: HTTP status code
            duration: Request duration in seconds
        """
        # Record request count
        http_requests_total.labels(
            method=method,
            endpoint=endpoint,
            status_code=str(status_code),
        ).inc()

        # Record request duration
        http_request_duration_seconds.labels(
            method=method,
            endpoint=endpoint,
        ).observe(duration)

        # Log slow requests (>1 second)
        if duration > 1.0:
            logger.warning(
                f"Slow request: {method} {endpoint} took {duration:.3f}s",
                extra={
                    "method": method,
                    "endpoint": endpoint,
                    "status_code": status_code,
                    "duration_seconds": duration,
                },
            )
