"""Unit tests for @require_scopes with stored app passwords (Login Flow v2).

Tests the third enforcement mode in scope_authorization.py that checks
application-level scopes stored alongside app passwords.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context

from nextcloud_mcp_server.auth.scope_authorization import (
    ProvisioningRequiredError,
    _get_stored_scopes,
    _scope_cache,
    require_scopes,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_scope_cache():
    """Clear scope cache before each test."""
    _scope_cache.clear()
    yield
    _scope_cache.clear()


async def test_get_stored_scopes_with_scopes():
    """Test getting specific scopes from storage."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = {
        "app_password": "xxxxx",
        "scopes": ["notes.read", "calendar.read"],
        "username": "alice",
        "created_at": 1000,
        "updated_at": 1000,
    }

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("alice")

    assert result == ["notes.read", "calendar.read"]


async def test_get_stored_scopes_null_scopes():
    """Test that NULL scopes returns 'all'."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = {
        "app_password": "xxxxx",
        "scopes": None,
        "username": "bob",
        "created_at": 1000,
        "updated_at": 1000,
    }

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("bob")

    assert result == "all"


async def test_get_stored_scopes_no_password():
    """Test that missing app password returns None."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.return_value = None

    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
        return_value=mock_storage,
    ):
        result = await _get_stored_scopes("nobody")

    assert result is None


async def test_get_stored_scopes_storage_error():
    """Test that storage errors propagate to the caller."""
    mock_storage = AsyncMock()
    mock_storage.get_app_password_with_scopes.side_effect = RuntimeError("DB error")

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_shared_storage",
            return_value=mock_storage,
        ),
        pytest.raises(RuntimeError, match="DB error"),
    ):
        await _get_stored_scopes("alice")


@pytest.fixture(autouse=True)
def authenticated_request():
    """Populate the auth contextvar as AuthContextMiddleware does per request.

    ``require_scopes`` reads the token from this contextvar. Fabricating a
    ``request_context.access_token`` attribute instead (as these tests once
    did) asserts a contract that does not exist at runtime — ``RequestContext``
    has no such field, so the decorator silently allowed every call.
    The token's own scopes don't matter here: the Login-Flow-v2 branch checks
    the stored app-password scopes instead.
    """
    token = AccessToken(
        token="opaque",
        client_id="test-client",
        scopes=[],
        expires_at=None,
        resource="alice",
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _make_login_flow_ctx() -> MagicMock:
    """Build a minimal Context shaped like the Login-Flow-v2 / OAuth case."""
    ctx = MagicMock()
    ctx.request_context = SimpleNamespace()
    ctx.elicit = AsyncMock(return_value=SimpleNamespace(action="accept", data=None))
    return ctx


async def test_decorator_elicits_and_uses_retry_message_when_user_accepts():
    """When the elicit returns "accepted" the raised error must tell the user
    to retry — *not* "call nc_auth_provision_access". The latter would loop
    an LLM that just acknowledged the elicitation prompt.

    See PR #757 review feedback (cbcoutinho/nextcloud-mcp-server#757).
    """
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.read")
    async def fake_tool_missing_pwd(ctx: Context):  # noqa: ARG001
        return "ok"

    fake_settings = SimpleNamespace(enable_login_flow=True)
    elicit_mock = AsyncMock(return_value="accepted")

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=fake_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.scope_authorization._get_stored_scopes",
            return_value=None,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
            return_value="alice",
        ),
        # Patch the elicitation module (not scope_authorization) because the
        # decorator does a local import of present_provisioning_required to
        # avoid a circular import, so the name is re-fetched at call-time.
        patch(
            "nextcloud_mcp_server.auth.elicitation.present_provisioning_required",
            elicit_mock,
        ),
        pytest.raises(ProvisioningRequiredError) as exc_info,
    ):
        await fake_tool_missing_pwd(ctx=ctx)

    elicit_mock.assert_awaited_once_with(ctx)
    msg = str(exc_info.value)
    assert "retry the request" in msg
    assert "nc_auth_provision_access" not in msg


async def test_decorator_uses_legacy_message_when_elicitation_unsupported():
    """When the elicit helper returns "message_only" (client lacks elicit
    support), the raised error must keep the existing
    "call nc_auth_provision_access" instruction so an agent has something
    actionable. Mirrors the "accepted" case but for the fallback branch."""
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.read")
    async def fake_tool_missing_pwd_no_elicit(ctx: Context):  # noqa: ARG001
        return "ok"

    fake_settings = SimpleNamespace(enable_login_flow=True)
    elicit_mock = AsyncMock(return_value="message_only")

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=fake_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.scope_authorization._get_stored_scopes",
            return_value=None,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
            return_value="alice",
        ),
        patch(
            "nextcloud_mcp_server.auth.elicitation.present_provisioning_required",
            elicit_mock,
        ),
        pytest.raises(ProvisioningRequiredError) as exc_info,
    ):
        await fake_tool_missing_pwd_no_elicit(ctx=ctx)

    elicit_mock.assert_awaited_once_with(ctx)
    msg = str(exc_info.value)
    assert "nc_auth_provision_access" in msg
    assert "retry the request" not in msg


async def test_decorator_uses_legacy_message_when_user_declines():
    """When the elicit returns "declined" the user has explicitly declined the
    provisioning prompt. They still need to provision before the tool can run,
    so the raised error keeps the "call nc_auth_provision_access" instruction
    (same fall-through branch as message_only). Lock in this behaviour so a
    future refactor that splits the else-branch can't silently change it."""
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.read")
    async def fake_tool_user_declined(ctx: Context):  # noqa: ARG001
        return "ok"

    fake_settings = SimpleNamespace(enable_login_flow=True)
    elicit_mock = AsyncMock(return_value="declined")

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=fake_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.scope_authorization._get_stored_scopes",
            return_value=None,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
            return_value="alice",
        ),
        patch(
            "nextcloud_mcp_server.auth.elicitation.present_provisioning_required",
            elicit_mock,
        ),
        pytest.raises(ProvisioningRequiredError) as exc_info,
    ):
        await fake_tool_user_declined(ctx=ctx)

    elicit_mock.assert_awaited_once_with(ctx)
    msg = str(exc_info.value)
    assert "nc_auth_provision_access" in msg
    assert "retry the request" not in msg


async def test_decorator_uses_legacy_message_when_user_cancels():
    """When the elicit returns "cancelled" (user dismissed the prompt without
    answering), the user is still unprovisioned and needs to call the auth
    tool. Same fall-through as declined and message_only — locked in by an
    explicit test so the three callers don't drift apart in a future refactor."""
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.read")
    async def fake_tool_user_cancelled(ctx: Context):  # noqa: ARG001
        return "ok"

    fake_settings = SimpleNamespace(enable_login_flow=True)
    elicit_mock = AsyncMock(return_value="cancelled")

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=fake_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.scope_authorization._get_stored_scopes",
            return_value=None,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
            return_value="alice",
        ),
        patch(
            "nextcloud_mcp_server.auth.elicitation.present_provisioning_required",
            elicit_mock,
        ),
        pytest.raises(ProvisioningRequiredError) as exc_info,
    ):
        await fake_tool_user_cancelled(ctx=ctx)

    elicit_mock.assert_awaited_once_with(ctx)
    msg = str(exc_info.value)
    assert "nc_auth_provision_access" in msg
    assert "retry the request" not in msg


async def test_decorator_does_not_elicit_when_scopes_only_partially_missing():
    """When the user *has* an app password but is missing some requested
    scopes, the decorator raises InsufficientScopeError (step-up auth),
    not ProvisioningRequiredError — and must not elicit the
    provisioning-required prompt, because the user is already provisioned.
    """
    from nextcloud_mcp_server.auth.scope_authorization import (
        InsufficientScopeError,
    )

    ctx = _make_login_flow_ctx()

    @require_scopes("notes.write")
    async def fake_tool_missing_scope(ctx: Context):  # noqa: ARG001
        return "ok"

    fake_settings = SimpleNamespace(enable_login_flow=True)
    elicit_mock = AsyncMock()

    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization.get_settings",
            return_value=fake_settings,
        ),
        patch(
            "nextcloud_mcp_server.auth.scope_authorization._get_stored_scopes",
            return_value=["notes.read"],  # has read, lacks write
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
            return_value="alice",
        ),
        patch(
            "nextcloud_mcp_server.auth.elicitation.present_provisioning_required",
            elicit_mock,
        ),
        pytest.raises(InsufficientScopeError),
    ):
        await fake_tool_missing_scope(ctx=ctx)

    elicit_mock.assert_not_awaited()
