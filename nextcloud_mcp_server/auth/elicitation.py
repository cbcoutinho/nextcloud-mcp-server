"""MCP elicitation helpers for Login Flow v2.

Provides a unified way to present login URLs to users, using MCP elicitation
when the client supports it, or falling back to returning the URL in a message.
"""

import logging
from typing import Any

from mcp.server.mcpserver import Context
from mcp.shared.exceptions import NoBackChannelError
from pydantic import BaseModel, Field

from nextcloud_mcp_server.astrolabe_links import astrolabe_browser_base
from nextcloud_mcp_server.observability.metrics import record_elicitation

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

    Delegates the base-URL resolution and its scheme check to
    :func:`astrolabe_browser_base`, so that logic lives in one place. Returns
    None when no browser-reachable Nextcloud URL is configured — the caller then
    renders the tool-only fallback message instead of a broken link.
    """
    base = astrolabe_browser_base()
    if base is None:
        return None
    return f"{base}{ASTROLABE_SETTINGS_PATH}"


async def _run_elicit(
    ctx: Context,
    message: str,
    schema: type[BaseModel],
    *,
    prompt: str,
) -> tuple[str, Any]:
    """Shared elicit-or-fallback flow used by all elicitation prompts.

    Returns ``(outcome, result)`` where ``outcome`` is one of
    ``"accepted"`` / ``"declined"`` / ``"cancelled"`` / ``"message_only"``.
    ``result`` is the underlying ``ctx.elicit()`` return value when the
    elicitation actually ran (any of the first three outcomes), else None.
    Callers needing post-accept inspection (e.g. the data-acknowledged
    warning in :func:`present_login_url`) read it from ``result``.

    ``prompt`` identifies which prompt is running ("login_flow" /
    "provisioning_required"); it labels both the log lines and the
    ``mcp_elicitation_total`` metric.

    Every fallback here is deliberately silent to the caller — the point is to
    degrade rather than fail — which is exactly why each one is counted. On
    protocol 2026-07-28 there is no back-channel and no server-initiated
    request, so ``ctx.elicit()`` cannot run at all; that lands in
    ``reason="no_back_channel"``. Watching that counter rise against
    ``"accepted"`` is how the era transition becomes visible.
    """
    if not hasattr(ctx, "elicit"):
        logger.debug(
            "Elicitation not available on context — message_only fallback (%s)",
            prompt,
        )
        record_elicitation(prompt, "message_only", "no_elicit_attr")
        return "message_only", None

    try:
        result = await ctx.elicit(message=message, schema=schema)
    except NoBackChannelError:
        # 2026-07-28 has no server-initiated requests, so ctx.elicit() cannot
        # reach the client at all (also true of a legacy session against a
        # stateless_http / json_response server). The login URL still travels in
        # the returned message, which is the whole point of this fallback.
        logger.debug(
            "No back-channel on this connection — message_only fallback (%s)",
            prompt,
        )
        record_elicitation(prompt, "message_only", "no_back_channel")
        return "message_only", None
    except NotImplementedError:
        logger.debug(
            "Elicitation not supported by client — message_only fallback (%s)",
            prompt,
        )
        record_elicitation(prompt, "message_only", "not_implemented")
        return "message_only", None
    except Exception as e:
        logger.warning(
            "Elicitation failed unexpectedly for %s (%s: %s), "
            "falling back to message_only",
            prompt,
            type(e).__name__,
            e,
        )
        record_elicitation(prompt, "message_only", "error")
        return "message_only", None

    if result.action == "accept":
        logger.info("User acknowledged %s", prompt)
        record_elicitation(prompt, "accepted")
        return "accepted", result
    if result.action == "decline":
        logger.info("User declined %s", prompt)
        record_elicitation(prompt, "declined")
        return "declined", result
    logger.info("User cancelled %s", prompt)
    record_elicitation(prompt, "cancelled")
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
        prompt="login_flow",
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
        prompt="provisioning_required",
    )
    return outcome
