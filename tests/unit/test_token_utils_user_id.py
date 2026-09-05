"""Unit tests for ``extract_user_id_from_token`` (PR #758 follow-up review).

The function used to silently fall back to ``"default_user"`` whenever the
verified access token had no ``sub`` claim. In a multi-tenant deployment
that would let a malformed IdP token bucket every request under a single
sentinel user, risking cross-tenant data exposure. The fix is to keep the
no-token fallback (BasicAuth mode legitimately calls this without an
OAuth identity) but raise ``MCPError`` whenever an access token is
present and ``resource`` is empty.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.auth.provider import AccessToken
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.auth.token_utils import extract_user_id_from_token

pytestmark = pytest.mark.unit


def _token(resource: str | None = "alice") -> AccessToken:
    return AccessToken(
        token="t",
        client_id="test-client",
        scopes=["openid"],
        expires_at=int(time.time() + 3600),
        resource=resource,
    )


async def test_returns_user_id_when_token_has_sub():
    """Happy path: verified access token with sub → returns the sub."""
    with patch(
        "nextcloud_mcp_server.auth.token_utils.get_access_token",
        return_value=_token("alice"),
    ):
        user_id = await extract_user_id_from_token(MagicMock())

    assert user_id == "alice"


async def test_returns_default_user_when_no_access_token():
    """BasicAuth mode: get_access_token() returns None → sentinel.

    BasicAuth deployments don't issue OAuth tokens; the sentinel lets
    BasicAuth-aware callers branch on it. Removing this fallback would
    break the BasicAuth path.
    """
    with patch(
        "nextcloud_mcp_server.auth.token_utils.get_access_token",
        return_value=None,
    ):
        user_id = await extract_user_id_from_token(MagicMock())

    assert user_id == "default_user"


async def test_raises_when_token_present_but_resource_empty():
    """Token present but ``resource`` empty → fail closed with MCPError.

    Pins the PR #758 follow-up review fix: a malformed IdP token must
    not silently funnel users into a shared ``"default_user"`` SQLite
    bucket.
    """
    with patch(
        "nextcloud_mcp_server.auth.token_utils.get_access_token",
        return_value=_token(""),
    ):
        with pytest.raises(MCPError, match="Cannot determine user identity"):
            await extract_user_id_from_token(MagicMock())


async def test_raises_when_resource_is_none():
    """Same fail-closed behaviour when ``resource`` is None rather than ''."""
    with patch(
        "nextcloud_mcp_server.auth.token_utils.get_access_token",
        return_value=_token(None),
    ):
        with pytest.raises(MCPError, match="Cannot determine user identity"):
            await extract_user_id_from_token(MagicMock())
