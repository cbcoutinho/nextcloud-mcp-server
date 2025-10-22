"""OAuth authentication components for Nextcloud MCP server."""

from .bearer_auth import BearerAuth
from .client_registration import load_or_register_client, register_client
from .context_helper import get_client_from_context
from .scope_authorization import (
    InsufficientScopeError,
    ScopeAuthorizationError,
    check_scopes,
    get_access_token_scopes,
    get_required_scopes,
    has_required_scopes,
    require_scopes,
)
from .token_verifier import NextcloudTokenVerifier

__all__ = [
    "BearerAuth",
    "NextcloudTokenVerifier",
    "register_client",
    "load_or_register_client",
    "get_client_from_context",
    "require_scopes",
    "ScopeAuthorizationError",
    "InsufficientScopeError",
    "check_scopes",
    "get_access_token_scopes",
    "get_required_scopes",
    "has_required_scopes",
]
