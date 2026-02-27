"""MCP elicitation helpers for Login Flow v2.

Provides a unified way to present login URLs to users, using MCP elicitation
when the client supports it, or falling back to returning the URL in a message.
"""

import logging

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class LoginFlowConfirmation(BaseModel):
    """Schema for Login Flow v2 confirmation elicitation."""

    acknowledged: bool = Field(
        default=False,
        description="Check this box after completing login at the provided URL",
    )


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

    try:
        result = await ctx.elicit(
            message=message,
            schema=LoginFlowConfirmation,
        )

        if result.action == "accept":
            logger.info("User acknowledged login flow completion")
            return "accepted"
        elif result.action == "decline":
            logger.info("User declined login flow")
            return "declined"
        else:
            logger.info("User cancelled login flow")
            return "cancelled"

    except Exception as e:
        # Elicitation not supported by this client - fall back to message
        logger.debug(f"Elicitation not available ({e}), returning URL in message")
        return "message_only"
