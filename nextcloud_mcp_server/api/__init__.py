"""Management API for Nextcloud MCP Server.

Provides REST endpoints for the Nextcloud PHP app to query server status,
user sessions, and vector sync metrics. All endpoints use OAuth bearer token
authentication via the UnifiedTokenVerifier.

This package is organized into modules by domain:
- management.py: Server status, user sessions, shared helpers
- passwords.py: App password provisioning for multi-user BasicAuth
- apps.py: Installed Nextcloud apps

The semantic-search endpoints (visualization.py, vector_sync.py, sar.py) are
deliberately not re-exported: they import the optional vector stack, and this
package must stay importable without it. Import them from their modules.
"""

from nextcloud_mcp_server.api.access import (
    get_user_access,
    list_supported_scopes,
    update_user_scopes,
)
from nextcloud_mcp_server.api.apps import get_installed_apps

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

__all__ = [
    # Access endpoints (from access.py)
    "get_user_access",
    "update_user_scopes",
    "list_supported_scopes",
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
    # Installed-apps endpoint (from apps.py)
    "get_installed_apps",
]
