"""Smithery-specific entrypoint for stateless deployment.

ADR-016: This entrypoint is used when deploying on Smithery's hosting platform.
It configures the server for stateless operation with per-session authentication.

Features disabled in Smithery mode:
- Vector sync / semantic search (no persistent storage)
- Admin UI at /app (no webhooks, no vector viz)
- OAuth provisioning tools (no token storage)

Features enabled:
- Core Nextcloud tools (notes, calendar, contacts, files, deck, tables, cookbook)
- Per-session app password authentication via Smithery configSchema
- Health check endpoints (/health/live, /health/ready)
"""

import logging
import os

import uvicorn

from nextcloud_mcp_server.config import setup_logging

logger = logging.getLogger(__name__)


def main():
    """Start the MCP server in Smithery stateless mode."""
    # Setup logging first
    setup_logging()

    # Force stateless mode environment variables
    os.environ["SMITHERY_DEPLOYMENT"] = "true"
    os.environ["VECTOR_SYNC_ENABLED"] = "false"

    logger.info("Starting Nextcloud MCP Server in Smithery stateless mode")

    # Import app after setting environment variables
    from nextcloud_mcp_server.app import get_app

    # Create the app with streamable-http transport (required for Smithery)
    app = get_app(transport="streamable-http")

    # Smithery sets PORT environment variable
    port = int(os.environ.get("PORT", 8081))

    logger.info(f"Listening on port {port}")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        # Disable access log for cleaner output
        access_log=False,
    )


if __name__ == "__main__":
    main()
