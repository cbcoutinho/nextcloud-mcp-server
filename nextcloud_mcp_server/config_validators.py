"""Configuration validation and mode detection for the MCP server.

This module provides:
- Mode detection based on configuration
- Configuration validation with clear error messages
- Single source of truth for deployment mode requirements

See ADR-020 for detailed architecture and deployment mode documentation.
"""

import logging
from dataclasses import dataclass
from enum import Enum

from nextcloud_mcp_server.config import Settings

logger = logging.getLogger(__name__)


class AuthMode(Enum):
    """Authentication mode for the MCP server.

    Determines how users authenticate and how the server accesses Nextcloud.
    """

    SINGLE_USER_BASIC = "single_user_basic"
    MULTI_USER_BASIC = "multi_user_basic"
    OAUTH_SINGLE_AUDIENCE = "oauth_single"
    OAUTH_TOKEN_EXCHANGE = "oauth_exchange"
    SMITHERY_STATELESS = "smithery"


@dataclass
class ModeRequirements:
    """Requirements for a deployment mode.

    Attributes:
        required: Configuration variables that must be set
        optional: Configuration variables that may be set
        forbidden: Configuration variables that should not be set
        conditional: Additional requirements based on feature flags
                     Format: {feature_flag: [required_vars]}
        description: Human-readable description of the mode
    """

    required: list[str]
    optional: list[str]
    forbidden: list[str]
    conditional: dict[str, list[str]]
    description: str


# Mode requirements definition
MODE_REQUIREMENTS: dict[AuthMode, ModeRequirements] = {
    AuthMode.SINGLE_USER_BASIC: ModeRequirements(
        required=["nextcloud_host", "nextcloud_username", "nextcloud_password"],
        optional=[
            "vector_sync_enabled",
            "qdrant_url",
            "qdrant_location",
            "ollama_base_url",
            "ollama_embedding_model",
            "openai_api_key",
            "openai_embedding_model",
            "document_chunk_size",
            "document_chunk_overlap",
        ],
        forbidden=[
            "enable_multi_user_basic_auth",
            "enable_token_exchange",
            "oidc_client_id",
            "oidc_client_secret",
        ],
        conditional={
            "vector_sync_enabled": [
                # Either qdrant_url OR qdrant_location (checked in Settings.__post_init__)
                # At least one embedding provider (ollama_base_url OR openai_api_key)
            ],
        },
        description="Single-user deployment with BasicAuth credentials. "
        "Suitable for personal Nextcloud instances and local development.",
    ),
    AuthMode.MULTI_USER_BASIC: ModeRequirements(
        required=["nextcloud_host", "enable_multi_user_basic_auth"],
        optional=[
            # Background sync with app passwords (via Astrolabe)
            "enable_offline_access",
            "token_encryption_key",
            "token_storage_db",
            "oidc_client_id",
            "oidc_client_secret",
            # Vector sync
            "vector_sync_enabled",
            "qdrant_url",
            "qdrant_location",
            "ollama_base_url",
            "ollama_embedding_model",
            "openai_api_key",
            "openai_embedding_model",
        ],
        forbidden=[
            "nextcloud_username",
            "nextcloud_password",
            "enable_token_exchange",
        ],
        conditional={
            "enable_offline_access": [
                "oidc_client_id",
                "oidc_client_secret",
                "token_encryption_key",
                "token_storage_db",
            ],
            "vector_sync_enabled": [
                # Requires offline access for background sync
                "enable_offline_access",
            ],
        },
        description="Multi-user deployment with BasicAuth pass-through. "
        "Users provide credentials in request headers. "
        "Optional background sync using app passwords stored via Astrolabe.",
    ),
    AuthMode.OAUTH_SINGLE_AUDIENCE: ModeRequirements(
        required=["nextcloud_host"],
        optional=[
            # OAuth credentials (uses DCR if not provided)
            "oidc_client_id",
            "oidc_client_secret",
            "oidc_discovery_url",
            # Offline access
            "enable_offline_access",
            "token_encryption_key",
            "token_storage_db",
            # Vector sync
            "vector_sync_enabled",
            "qdrant_url",
            "qdrant_location",
            "ollama_base_url",
            "ollama_embedding_model",
            "openai_api_key",
            "openai_embedding_model",
            # Scopes
            "nextcloud_oidc_scopes",
        ],
        forbidden=[
            "nextcloud_username",
            "nextcloud_password",
            "enable_token_exchange",
            "enable_multi_user_basic_auth",
        ],
        conditional={
            "enable_offline_access": [
                "token_encryption_key",
                "token_storage_db",
            ],
            "vector_sync_enabled": [
                "enable_offline_access",  # Background sync requires refresh tokens
            ],
        },
        description="OAuth multi-user deployment with single-audience tokens. "
        "Tokens work for both MCP server and Nextcloud APIs (pass-through). "
        "Uses Dynamic Client Registration if credentials not provided.",
    ),
    AuthMode.OAUTH_TOKEN_EXCHANGE: ModeRequirements(
        required=["nextcloud_host", "enable_token_exchange"],
        optional=[
            # OAuth credentials
            "oidc_client_id",
            "oidc_client_secret",
            "oidc_discovery_url",
            # Token exchange settings
            "token_exchange_cache_ttl",
            # Offline access
            "enable_offline_access",
            "token_encryption_key",
            "token_storage_db",
            # Vector sync
            "vector_sync_enabled",
            "qdrant_url",
            "qdrant_location",
            "ollama_base_url",
            "ollama_embedding_model",
            "openai_api_key",
            "openai_embedding_model",
        ],
        forbidden=[
            "nextcloud_username",
            "nextcloud_password",
            "enable_multi_user_basic_auth",
        ],
        conditional={
            "enable_offline_access": [
                "token_encryption_key",
                "token_storage_db",
            ],
            "vector_sync_enabled": [
                "enable_offline_access",
            ],
        },
        description="OAuth multi-user deployment with token exchange (RFC 8693). "
        "MCP tokens are separate from Nextcloud tokens. "
        "Server exchanges MCP token for Nextcloud token on each request.",
    ),
    AuthMode.SMITHERY_STATELESS: ModeRequirements(
        required=[],  # All config from session URL params
        optional=[],
        forbidden=[
            "nextcloud_host",
            "nextcloud_username",
            "nextcloud_password",
            "enable_multi_user_basic_auth",
            "enable_token_exchange",
            "enable_offline_access",
            "vector_sync_enabled",
            "oidc_client_id",
            "oidc_client_secret",
        ],
        conditional={},
        description="Stateless multi-tenant deployment for Smithery platform. "
        "Configuration comes from session URL parameters. "
        "No persistent storage, no OAuth, no vector sync.",
    ),
}


def detect_auth_mode(settings: Settings) -> AuthMode:
    """Detect authentication mode from configuration.

    Mode detection priority (most specific to most general):
    1. Smithery (explicit flag)
    2. Token exchange (most specific OAuth mode)
    3. Multi-user BasicAuth
    4. Single-user BasicAuth
    5. OAuth single-audience (default OAuth mode)

    Args:
        settings: Application settings

    Returns:
        Detected AuthMode
    """
    # Check for Smithery mode (explicit environment variable)
    # Note: This checks the environment directly, not settings
    # because Smithery mode has no settings-based config
    import os

    if os.getenv("SMITHERY_DEPLOYMENT", "false").lower() == "true":
        return AuthMode.SMITHERY_STATELESS

    # Check for token exchange (most specific OAuth mode)
    if settings.enable_token_exchange:
        return AuthMode.OAUTH_TOKEN_EXCHANGE

    # Check for multi-user BasicAuth
    if settings.enable_multi_user_basic_auth:
        return AuthMode.MULTI_USER_BASIC

    # Check for single-user BasicAuth (explicit credentials)
    if settings.nextcloud_username and settings.nextcloud_password:
        return AuthMode.SINGLE_USER_BASIC

    # Default: OAuth single-audience mode
    # This is the safest multi-user mode (no credential storage)
    return AuthMode.OAUTH_SINGLE_AUDIENCE


def validate_configuration(settings: Settings) -> tuple[AuthMode, list[str]]:
    """Validate configuration for detected mode.

    Args:
        settings: Application settings

    Returns:
        Tuple of (detected_mode, list_of_errors)
        Empty list means valid configuration.
    """
    mode = detect_auth_mode(settings)
    requirements = MODE_REQUIREMENTS[mode]
    errors: list[str] = []

    logger.debug(f"Validating configuration for mode: {mode.value}")

    # Check required variables
    for var in requirements.required:
        value = getattr(settings, var, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(
                f"[{mode.value}] Missing required configuration: {var.upper()}"
            )

    # Check forbidden variables
    for var in requirements.forbidden:
        value = getattr(settings, var, None)
        # For bools, check if True (forbidden means must be False/unset)
        # For strings, check if non-empty
        is_set = False
        if isinstance(value, bool):
            is_set = value is True
        elif isinstance(value, str):
            is_set = bool(value.strip())
        elif value is not None:
            is_set = True

        if is_set:
            errors.append(
                f"[{mode.value}] Forbidden configuration: {var.upper()} "
                f"should not be set in this mode"
            )

    # Check conditional requirements
    for condition, required_vars in requirements.conditional.items():
        # Check if the condition is enabled
        condition_value = getattr(settings, condition, None)
        is_enabled = False

        if isinstance(condition_value, bool):
            is_enabled = condition_value is True
        elif isinstance(condition_value, str):
            is_enabled = bool(condition_value.strip())
        elif condition_value is not None:
            is_enabled = True

        if is_enabled:
            # Check that all required vars for this condition are set
            for var in required_vars:
                value = getattr(settings, var, None)

                # For boolean requirements, check that they are True (not just set)
                if hasattr(Settings, var):
                    field_type = type(getattr(Settings(), var, None))
                    if field_type is bool:
                        if value is not True:
                            errors.append(
                                f"[{mode.value}] {var.upper()} must be enabled when "
                                f"{condition.upper()} is enabled"
                            )
                        continue

                # For non-boolean requirements, check that they are set
                if value is None or (isinstance(value, str) and not value.strip()):
                    errors.append(
                        f"[{mode.value}] {var.upper()} is required when "
                        f"{condition.upper()} is enabled"
                    )

    # Special validations for specific modes
    if mode == AuthMode.SINGLE_USER_BASIC:
        # Validate that NEXTCLOUD_HOST doesn't have trailing slash
        if settings.nextcloud_host and settings.nextcloud_host.endswith("/"):
            errors.append(
                f"[{mode.value}] NEXTCLOUD_HOST should not have trailing slash: "
                f"{settings.nextcloud_host}"
            )

    if mode in [
        AuthMode.OAUTH_SINGLE_AUDIENCE,
        AuthMode.OAUTH_TOKEN_EXCHANGE,
    ]:
        # If OAuth credentials not provided, DCR must be available
        # (This is a runtime check, not a config check, so we just warn)
        if not settings.oidc_client_id or not settings.oidc_client_secret:
            logger.info(
                f"[{mode.value}] OAuth credentials not configured. "
                "Will attempt Dynamic Client Registration (DCR) at startup."
            )

    if mode == AuthMode.MULTI_USER_BASIC:
        # Validate that if offline access enabled, we have OAuth credentials
        if settings.enable_offline_access:
            if not settings.oidc_client_id or not settings.oidc_client_secret:
                errors.append(
                    f"[{mode.value}] NEXTCLOUD_OIDC_CLIENT_ID and "
                    "NEXTCLOUD_OIDC_CLIENT_SECRET are required when "
                    "ENABLE_OFFLINE_ACCESS is enabled (for app password retrieval)"
                )

        # Validate vector sync requirements
        if settings.vector_sync_enabled and not settings.enable_offline_access:
            errors.append(
                f"[{mode.value}] ENABLE_OFFLINE_ACCESS must be enabled when "
                "VECTOR_SYNC_ENABLED is true (background sync requires "
                "app passwords or refresh tokens)"
            )

    # Note: Embedding provider validation removed - Simple provider is always
    # available as fallback (ADR-015). Users can optionally configure Ollama or OpenAI
    # for better quality embeddings.

    return mode, errors


def get_mode_summary(mode: AuthMode) -> str:
    """Get human-readable summary of a deployment mode.

    Args:
        mode: Deployment mode

    Returns:
        Multi-line string describing the mode
    """
    requirements = MODE_REQUIREMENTS[mode]

    summary_lines = [
        f"Mode: {mode.value}",
        f"Description: {requirements.description}",
        "",
        "Required configuration:",
    ]

    if requirements.required:
        for var in requirements.required:
            summary_lines.append(f"  - {var.upper()}")
    else:
        summary_lines.append("  (none - configured via session)")

    summary_lines.append("")
    summary_lines.append("Optional configuration:")

    if requirements.optional:
        for var in requirements.optional:
            summary_lines.append(f"  - {var.upper()}")
    else:
        summary_lines.append("  (none)")

    if requirements.conditional:
        summary_lines.append("")
        summary_lines.append("Conditional requirements:")
        for condition, vars in requirements.conditional.items():
            summary_lines.append(f"  When {condition.upper()} is enabled:")
            for var in vars:
                summary_lines.append(f"    - {var.upper()}")

    return "\n".join(summary_lines)
