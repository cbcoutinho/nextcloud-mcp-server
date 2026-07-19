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

from opentelemetry.trace import Status, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.observability.metrics import (
    http_request_duration_seconds,
    http_requests_in_progress,
    http_requests_total,
)
from nextcloud_mcp_server.observability.tracing import (
    add_span_attribute,
    trace_server_request,
)

logger = logging.getLogger(__name__)

# Nextcloud's reqId is 20 chars; the cap is slack for other callers, not a
# format assumption. Bounds what an untrusted header can put on every span.
_MAX_REQUEST_ID_LEN = 128


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

        # Skip tracing for health/metrics/polling endpoints to reduce noise
        should_trace = not (
            path.startswith("/health/")
            or path == "/metrics"
            or path == "/app/vector-sync/status"
        )

        try:
            if should_trace:
                # SERVER-kind span parented to the caller's traceparent, so an
                # Astrolabe request and the work it triggers here land in one
                # trace instead of two unrelated ones.
                #
                # http.route carries the normalized template (the same label the
                # metrics use), not the raw path: it is what makes RED metrics
                # and "show me 5xx by route" queries possible without the
                # cardinality blowup of per-id paths.
                with trace_server_request(
                    f"HTTP {method} {endpoint}",
                    carrier=request.headers,
                    attributes={
                        "http.method": method,
                        "http.route": endpoint,
                        "http.path": path,
                        "http.scheme": request.url.scheme,
                        "http.host": request.url.hostname,
                        **self._tenant_attributes(),
                        **self._correlation_attributes(request),
                    },
                ) as span:
                    # Process request
                    response = await call_next(request)

                    # Add response status to span
                    add_span_attribute("http.status_code", response.status_code)

                    # A handler that catches its own exception and returns a 500
                    # (which every /api/v1 handler does) never raises past this
                    # middleware, so the span would otherwise finish OK and be
                    # invisible to a `status=error` trace query — the exact
                    # blind spot this PR set out to close. Derive the span
                    # status from the response as well as tagging it.
                    if span is not None and response.status_code >= 500:
                        span.set_status(
                            Status(StatusCode.ERROR, f"HTTP {response.status_code}")
                        )

                    # Record metrics
                    duration = time.time() - start_time
                    self._record_request_metrics(
                        method=method,
                        endpoint=endpoint,
                        status_code=response.status_code,
                        duration=duration,
                    )

                    return response
            else:
                # No tracing for health/metrics endpoints, but still record metrics
                response = await call_next(request)

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

            # exception() over error(): this is the genuine crash path — an
            # exception that no handler caught — so it is the one place a
            # traceback is worth most, and the one place it was being dropped.
            # Also Sonar python:S8572.
            logger.exception(
                "Request failed: %s %s",
                method,
                path,
                extra={
                    "method": method,
                    "path": path,
                    "duration_seconds": duration,
                    # Same key the span carries, so a crash found in the logs
                    # can be tied back to the Nextcloud request that caused it
                    # even if the trace was dropped by sampling.
                    **self._correlation_attributes(request),
                },
            )

            # Re-raise exception to be handled by error middleware
            raise

        finally:
            # Decrement in-flight requests counter
            http_requests_in_progress.labels(method=method, endpoint=endpoint).dec()

    def _correlation_attributes(self, request: Request) -> dict[str, str]:
        """Caller-supplied identifiers to hang on the span.

        Astrolabe cannot export spans of its own — it runs on managed storage
        with no collector in reach — so it forwards ``X-Request-Id``, which is
        Nextcloud's ``reqId``: the value prefixing every line that request
        writes to ``nextcloud.log``. Recording it here is what lets a
        user-visible failure be traced from the Nextcloud log, through this
        server's spans, to the query that actually broke, without Astrolabe
        needing to emit a single span.

        When Astrolabe does gain an OTel setup, ``traceparent`` takes over and
        links the two halves properly; ``trace_server_request`` already
        extracts it, so nothing here changes.

        Capped because it lands in span attributes: an unbounded header from a
        caller should not be able to inflate every span it touches.
        """
        request_id = request.headers.get("x-request-id")
        if not request_id:
            return {}
        return {"client.request.id": request_id[:_MAX_REQUEST_ID_LEN]}

    def _tenant_attributes(self) -> dict[str, str]:
        """Tenant identity for the span, when this deployment has one.

        One Tempo/Loki stack aggregates every tenant, so without this a trace
        cannot be attributed to a tenant without correlating on pod name.
        Resolved per request rather than snapshotted at import so test overrides
        of the setting take effect (single-tenant deployments leave it unset).
        """
        tenant_id = get_settings().tenant_id
        return {"tenant.id": tenant_id} if tenant_id else {}

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
                "Slow request: %s %s took %ss",
                method,
                endpoint,
                format(duration, ".3f"),
                extra={
                    "method": method,
                    "endpoint": endpoint,
                    "status_code": status_code,
                    "duration_seconds": duration,
                },
            )
