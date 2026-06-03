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
    LOGIN_FLOW = "login_flow"


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
        required=["nextcloud_host"],
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
        ],
        conditional={
            "enable_offline_access": [
                # OAuth credentials validated separately (lines 397-406) with clearer error message
                "token_encryption_key",
                "token_storage_db",
            ],
            # Note: vector_sync_enabled (now ENABLE_SEMANTIC_SEARCH) automatically
            # enables background operations in multi-user modes. No explicit
            # enable_offline_access setting required.
        },
        description="Multi-user deployment with BasicAuth pass-through. "
        "Users provide credentials in request headers. "
        "Optional background sync using app passwords stored via Astrolabe.",
    ),
    AuthMode.LOGIN_FLOW: ModeRequirements(
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
        ],
        conditional={
            "enable_offline_access": [
                "token_encryption_key",
                "token_storage_db",
            ],
            # Note: vector_sync_enabled (now ENABLE_SEMANTIC_SEARCH) automatically
            # enables background operations in multi-user modes. No explicit
            # enable_offline_access setting required.
        },
        description="OAuth multi-user deployment using Login Flow v2 to acquire "
        "per-user Nextcloud app passwords via a browser flow. The MCP server "
        "is an OIDC relying party of a configurable IdP (Nextcloud's built-in "
        "OIDC by default; Keycloak, AWS Cognito, etc. via OIDC_DISCOVERY_URL). "
        "Uses Dynamic Client Registration if credentials not provided. "
        "Replaces the deprecated direct OAuth bearer-token pass-through which "
        "required unmerged user_oidc patches (see ADR-022).",
    ),
}


def detect_auth_mode(settings: Settings) -> AuthMode:
    """Detect authentication mode from configuration.

    Mode detection priority (ADR-021, updated for ADR-022):
    0. Explicit MCP_DEPLOYMENT_MODE (if set) — NEW in ADR-021
    1. Multi-user BasicAuth (only via explicit mode after ADR-022 follow-up)
    2. Single-user BasicAuth (auto-detected from credentials)
    3. Login Flow v2 (default — was OAuth single-audience pre-ADR-022)

    Pure function — the legacy-env-var deprecation and the derivation of
    `enable_login_flow` / `enable_multi_user_basic_auth` now happen in
    `Settings.__post_init__` so every Settings instance carries correct
    flags regardless of how it was constructed.

    Keep the resolution logic here in sync with `Settings.__post_init__`:
    both compute the canonical mode from `deployment_mode` (+ credentials
    as a fallback). When adding a new mode, update `mode_map` *and* the
    `__post_init__` resolution block in `config.py`.

    Args:
        settings: Application settings

    Returns:
        Detected AuthMode

    Raises:
        ValueError: If explicit deployment_mode is unrecognised.
    """

    # ADR-021: explicit deployment mode wins
    if settings.deployment_mode:
        mode_str = settings.deployment_mode.lower().strip()

        mode_map = {
            "single_user_basic": AuthMode.SINGLE_USER_BASIC,
            "multi_user_basic": AuthMode.MULTI_USER_BASIC,
            "login_flow": AuthMode.LOGIN_FLOW,
        }

        if mode_str not in mode_map:
            valid_modes = ", ".join(mode_map.keys())
            # ADR-022 migration hint: the most common upgrade pain is users
            # carrying MCP_DEPLOYMENT_MODE=oauth_single_audience over from
            # ADR-021. Surface a one-liner so they don't have to grep the
            # changelog.
            hint = (
                " (Note: 'oauth_single_audience' was renamed to 'login_flow' in ADR-022.)"
                if mode_str == "oauth_single_audience"
                else ""
            )
            raise ValueError(
                f"Invalid MCP_DEPLOYMENT_MODE: '{settings.deployment_mode}'. "
                f"Valid values: {valid_modes}.{hint}"
            )

        explicit_mode = mode_map[mode_str]
        logger.info("Using explicit deployment mode: %s", explicit_mode.value)
        return explicit_mode

    # Auto-detection (no explicit deployment_mode).
    # MULTI_USER_BASIC is no longer auto-detectable — the ENABLE_MULTI_USER_BASIC_AUTH
    # env-var alias was dropped in the ADR-022 follow-up, so the only way
    # to opt into that mode is `MCP_DEPLOYMENT_MODE=multi_user_basic`
    # (handled above). The legacy env var fails loudly in
    # `Settings.__post_init__`.

    if settings.nextcloud_username and settings.nextcloud_password:
        return AuthMode.SINGLE_USER_BASIC

    # Default: Login Flow v2 multi-user mode (browser-based app-password flow).
    return AuthMode.LOGIN_FLOW


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

    logger.debug("Validating configuration for mode: %s", mode.value)

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

    if mode == AuthMode.LOGIN_FLOW:
        # ADR-022 follow-up: the un-augmented OAuth bearer pass-through (the
        # old OAUTH_SINGLE_AUDIENCE without ENABLE_LOGIN_FLOW) needed unmerged
        # Nextcloud user_oidc patches and is no longer supported. The
        # `enable_login_flow` flag is now derived from the resolved mode in
        # `Settings.__post_init__`, so users only configure the mode — no
        # separate ENABLE_LOGIN_FLOW env var is needed.

        # If OAuth credentials not provided, DCR must be available
        # (This is a runtime check, not a config check, so we just warn)
        if not settings.oidc_client_id or not settings.oidc_client_secret:
            logger.info(
                "[%s] OAuth credentials not configured. Will attempt Dynamic Client Registration (DCR) at startup.",
                mode.value,
            )

    if mode == AuthMode.MULTI_USER_BASIC:
        # If background operations enabled, check for OAuth credentials (for app password retrieval)
        # Allow DCR as fallback, just like OAuth modes
        if settings.enable_offline_access:
            if not settings.oidc_client_id or not settings.oidc_client_secret:
                logger.info(
                    "[%s] OAuth credentials not configured. Will attempt Dynamic Client Registration (DCR) at startup (required for app password retrieval via Astrolabe).",
                    mode.value,
                )

        # Note: Vector sync no longer requires explicit ENABLE_OFFLINE_ACCESS setting
        # ENABLE_SEMANTIC_SEARCH (formerly VECTOR_SYNC_ENABLED) automatically enables
        # background operations in multi-user modes via smart dependency resolution
        # in config.py

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
