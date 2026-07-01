"""MCP elicitation helpers for Login Flow v2.

Provides a unified way to present login URLs to users, using MCP elicitation
when the client supports it, or falling back to returning the URL in a message.
"""

import logging
from typing import Any

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from nextcloud_mcp_server.config import get_settings

logger = logging.getLogger(__name__)

# Path of the Astrolabe Nextcloud app's settings UI. The full URL is
# reconstructed at elicitation time from settings.nextcloud_browser_url (the
# browser-reachable Nextcloud base URL) so the user gets a working link without
# needing a separate config knob. If the Astrolabe app is not installed this
# path will 404, and the user falls back to the nc_auth_provision_access tool
# path mentioned in the same message.
ASTROLABE_SETTINGS_PATH = "/index.php/apps/astrolabe/settings"


class LoginFlowConfirmation(BaseModel):
    """Schema for Login Flow v2 confirmation elicitation."""

    acknowledged: bool = Field(
        default=False,
        description="Check this box after completing login at the provided URL",
    )


class ProvisioningRequiredConfirmation(BaseModel):
    """Schema for the 'app password not provisioned' elicitation."""

    acknowledged: bool = Field(
        default=False,
        description="Check this box after enabling Nextcloud access",
    )


def _astrolabe_settings_url() -> str | None:
    """Construct the Astrolabe settings page URL from settings.

    Uses ``nextcloud_browser_url`` (the browser-reachable Nextcloud base URL:
    ``nextcloud_public_url`` → ``nextcloud_public_issuer_url`` → ``nextcloud_host``)
    so the link points at Nextcloud even in external-IdP deployments where the
    OAuth issuer URL is the IdP, not Nextcloud. Returns None if none is set (or
    set to the empty string), or if the configured base URL is missing an
    http:// or https:// scheme — in the latter case the caller renders the
    tool-only fallback message instead of a broken link.
    """
    settings = get_settings()
    base = (settings.nextcloud_browser_url or "").strip()
    if not base:
        return None
    if not base.startswith(("http://", "https://")):
        # Bare hostname (e.g. "internal:8080") would silently produce a
        # non-clickable URL. Surface the misconfiguration instead.
        logger.warning(
            "Cannot build Astrolabe settings URL: configured Nextcloud base URL "
            "%r is missing an http:// or https:// scheme. Falling back to the "
            "tool-only provisioning message.",
            base,
        )
        return None
    return f"{base.rstrip('/')}{ASTROLABE_SETTINGS_PATH}"


async def _run_elicit(
    ctx: Context,
    message: str,
    schema: type[BaseModel],
    *,
    log_label: str,
) -> tuple[str, Any]:
    """Shared elicit-or-fallback flow used by all elicitation prompts.

    Returns ``(outcome, result)`` where ``outcome`` is one of
    ``"accepted"`` / ``"declined"`` / ``"cancelled"`` / ``"message_only"``.
    ``result`` is the underlying ``ctx.elicit()`` return value when the
    elicitation actually ran (any of the first three outcomes), else None.
    Callers needing post-accept inspection (e.g. the data-acknowledged
    warning in :func:`present_login_url`) read it from ``result``.
    """
    if not hasattr(ctx, "elicit"):
        logger.debug(
            "Elicitation not available on context — message_only fallback (%s)",
            log_label,
        )
        return "message_only", None

    try:
        result = await ctx.elicit(message=message, schema=schema)
    except NotImplementedError:
        logger.debug(
            "Elicitation not supported by client — message_only fallback (%s)",
            log_label,
        )
        return "message_only", None
    except Exception as e:
        logger.warning(
            "Elicitation failed unexpectedly for %s (%s: %s), "
            "falling back to message_only",
            log_label,
            type(e).__name__,
            e,
        )
        return "message_only", None

    if result.action == "accept":
        logger.info("User acknowledged %s", log_label)
        return "accepted", result
    if result.action == "decline":
        logger.info("User declined %s", log_label)
        return "declined", result
    logger.info("User cancelled %s", log_label)
    return "cancelled", result


async def present_login_url(
    ctx: Context,
    login_url: str,
    message: str | None = None,
) -> str:
    """Present a login URL to the user via MCP elicitation or message.

    Tries MCP elicitation first (ctx.elicit) for interactive clients.
    Falls back to returning the URL as a plain message.

    Args:
        ctx: MCP context
        login_url: URL the user should open in their browser
        message: Optional custom message (defaults to standard Login Flow prompt)

    Returns:
        "accepted" if user acknowledged via elicitation,
        "declined" if user declined,
        "message_only" if elicitation not supported (URL returned in message)
    """
    if message is None:
        message = (
            f"Please log in to Nextcloud to grant access:\n\n"
            f"{login_url}\n\n"
            f"Open this URL in your browser, log in, and grant the requested permissions. "
            f"Then check the box below and click OK."
        )

    outcome, result = await _run_elicit(
        ctx,
        message,
        LoginFlowConfirmation,
        log_label="login flow completion",
    )

    if (
        outcome == "accepted"
        and result is not None
        and hasattr(result, "data")
        and not result.data.acknowledged
    ):
        # User clicked OK without ticking the box — login completion is still
        # verified via the LFv2 poller, so we proceed but flag it.
        logger.warning(
            "User accepted login flow without checking the acknowledged box — "
            "login completion will be verified via polling"
        )

    return outcome


async def present_provisioning_required(ctx: Context) -> str:
    """Elicit a provisioning prompt when a tool is called without an app password.

    Used by the ``@require_scopes`` decorator (Login Flow v2 path) to give
    the user a clickable Astrolabe settings URL — or a fallback instruction
    to call the ``nc_auth_provision_access`` MCP tool — instead of just
    raising a plain ``ProvisioningRequiredError`` text message that an LLM
    has to translate.

    The Astrolabe settings URL is reconstructed from
    ``settings.nextcloud_browser_url``; if Astrolabe is not installed the link
    404s and the user falls back to the tool path suggested in the same
    message.

    Returns:
        Same string contract as :func:`present_login_url`:
        ``"accepted"`` / ``"declined"`` / ``"cancelled"`` / ``"message_only"``.
    """
    settings_url = _astrolabe_settings_url()

    if settings_url:
        message = (
            "Nextcloud access is not yet provisioned for this user.\n\n"
            f"Open this URL to enable it via the Astrolabe app:\n\n{settings_url}\n\n"
            "If the Astrolabe app is not installed, ask your MCP client to call "
            "the `nc_auth_provision_access` tool instead — it will return a "
            "Login Flow v2 URL you can open in your browser.\n\n"
            "Then check the box below and retry the original request."
        )
    else:
        message = (
            "Nextcloud access is not yet provisioned for this user.\n\n"
            "Ask your MCP client to call the `nc_auth_provision_access` tool — "
            "it will return a Login Flow v2 URL you can open in your browser to "
            "grant access.\n\n"
            "Then check the box below and retry the original request."
        )

    outcome, _ = await _run_elicit(
        ctx,
        message,
        ProvisioningRequiredConfirmation,
        log_label="provisioning-required prompt",
    )
    return outcome
