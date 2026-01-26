"""Management API for Nextcloud MCP Server.

Provides REST endpoints for the Nextcloud PHP app to query server status,
user sessions, and vector sync metrics. All endpoints use OAuth bearer token
authentication via the UnifiedTokenVerifier.

This package is organized into modules by domain:
- management.py: Server status, user sessions, shared helpers
- passwords.py: App password provisioning for multi-user BasicAuth
- webhooks.py: Webhook registration management
- visualization.py: Search and PDF visualization endpoints
"""

# Re-export all public functions for backward compatibility
from nextcloud_mcp_server.api.management import (
    __version__,
    _parse_float_param,
    _parse_int_param,
    _sanitize_error_for_client,
    _validate_query_string,
    extract_bearer_token,
    get_server_status,
    get_user_session,
    get_vector_sync_status,
    revoke_user_access,
    validate_token_and_get_user,
)
from nextcloud_mcp_server.api.passwords import (
    delete_app_password,
    get_app_password_status,
    provision_app_password,
)
from nextcloud_mcp_server.api.visualization import (
    get_chunk_context,
    get_pdf_preview,
    unified_search,
    vector_search,
)
from nextcloud_mcp_server.api.webhooks import (
    create_webhook,
    delete_webhook,
    get_installed_apps,
    list_webhooks,
)

__all__ = [
    # Version
    "__version__",
    # Shared helpers (from management.py)
    "extract_bearer_token",
    "validate_token_and_get_user",
    "_sanitize_error_for_client",
    "_parse_int_param",
    "_parse_float_param",
    "_validate_query_string",
    # Status endpoints (from management.py)
    "get_server_status",
    "get_vector_sync_status",
    # Session endpoints (from management.py)
    "get_user_session",
    "revoke_user_access",
    # Password endpoints (from passwords.py)
    "provision_app_password",
    "get_app_password_status",
    "delete_app_password",
    # Webhook endpoints (from webhooks.py)
    "get_installed_apps",
    "list_webhooks",
    "create_webhook",
    "delete_webhook",
    # Visualization endpoints (from visualization.py)
    "unified_search",
    "vector_search",
    "get_chunk_context",
    "get_pdf_preview",
]
