"""
Observability module for the Nextcloud MCP Server.

This module provides:
- Prometheus metrics collection
- OpenTelemetry distributed tracing
- Enhanced structured logging with trace correlation
- Monitoring middleware for Starlette/FastAPI

Usage:
    from nextcloud_mcp_server.observability import setup_observability

    # In app.py lifespan
    setup_observability(app, config)
"""

from nextcloud_mcp_server.observability.logging_config import (
    get_uvicorn_logging_config,
    setup_logging,
)
from nextcloud_mcp_server.observability.metrics import setup_metrics
from nextcloud_mcp_server.observability.middleware import ObservabilityMiddleware
from nextcloud_mcp_server.observability.tracing import setup_tracing

__all__ = [
    "setup_logging",
    "get_uvicorn_logging_config",
    "setup_metrics",
    "setup_tracing",
    "ObservabilityMiddleware",
]
