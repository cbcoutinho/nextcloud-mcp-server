"""Unit tests for @require_scopes with stored app passwords (Login Flow v2).

Tests the third enforcement mode in scope_authorization.py that checks
application-level scopes stored alongside app passwords.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import Context

from nextcloud_mcp_server.auth.scope_authorization import (
    InsufficientScopeError,
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
    """Test that NULL scopes returns the 'all' sentinel.

    'all' means "this row places no *additional* restriction", not "everything
    is permitted" — the decorator still applies the token check afterwards.
    """
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

    The default token carries no scopes, which is enough for the tests that
    are denied by the *stored* layer before the token is ever consulted. Tests
    that reach the token check must set their own scopes via ``_token_scopes``.
    """
    with _token_scopes():
        yield


@contextmanager
def _token_scopes(*scopes: str):
    """Put a token carrying ``scopes`` on the auth contextvar."""
    token = AccessToken(
        token="opaque",
        client_id="test-client",
        scopes=list(scopes),
        expires_at=None,
        resource="alice",
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


@contextmanager
def _settings(*, login_flow: bool = True, offline_access: bool = False):
    """Patch get_settings for the decorator.

    ``enable_offline_access`` must be present: any call that falls through the
    stored-scope block reads it, so a double without it raises AttributeError
    instead of the assertion the test is making.
    """
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(
            enable_login_flow=login_flow,
            enable_offline_access=offline_access,
        ),
    ):
        yield


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


@contextmanager
def _stored(scopes):
    """Patch the stored app-password scopes and the user-id lookup."""
    with (
        patch(
            "nextcloud_mcp_server.auth.scope_authorization._get_stored_scopes",
            return_value=scopes,
        ),
        patch(
            "nextcloud_mcp_server.auth.token_utils.extract_user_id_from_token",
            return_value="alice",
        ),
    ):
        yield


async def test_null_stored_scopes_defer_to_token_and_allow():
    """A NULL stored grant restricts nothing, so a token carrying the scope wins.

    This is the GH #1277 case: Astrolabe-provisioned users hold NULL and their
    token carries semantic.read.
    """
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.read")
    async def fake_tool(ctx: Context):  # noqa: ARG001
        return "ok"

    with _settings(), _stored("all"), _token_scopes("notes.read"):
        assert await fake_tool(ctx=ctx) == "ok"


async def test_null_stored_scopes_defer_to_token_and_deny():
    """NULL is not a grant: a scope the token lacks is still denied.

    Before this behaviour existed, the 'all' sentinel returned early and a
    NULL-scoped user could call any tool regardless of their token — which
    also made list_tools (filtered on token scopes) disagree with tools/call.
    """
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.read")
    async def fake_tool(ctx: Context):  # noqa: ARG001
        return "ok"

    with (
        _settings(),
        _stored("all"),
        _token_scopes("openid"),
        pytest.raises(InsufficientScopeError) as exc_info,
    ):
        await fake_tool(ctx=ctx)

    assert exc_info.value.missing_scopes == ["notes.read"]


async def test_explicit_stored_scopes_still_gated_by_token():
    """Both layers must permit: a stored grant cannot exceed the token."""
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.write")
    async def fake_tool(ctx: Context):  # noqa: ARG001
        return "ok"

    with (
        _settings(),
        _stored(["notes.read", "notes.write"]),
        _token_scopes("notes.read"),
        pytest.raises(InsufficientScopeError) as exc_info,
    ):
        await fake_tool(ctx=ctx)

    assert exc_info.value.missing_scopes == ["notes.write"]


async def test_explicit_stored_scopes_pass_when_both_layers_allow():
    """The ordinary success path once both layers permit the scope."""
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.write")
    async def fake_tool(ctx: Context):  # noqa: ARG001
        return "ok"

    with _settings(), _stored(["notes.write"]), _token_scopes("notes.write"):
        assert await fake_tool(ctx=ctx) == "ok"


async def test_stored_denial_precedes_token_denial():
    """The stored layer denies first, and names the remedy for *that* layer.

    Locks the ordering so the two denial messages can't swap: only the stored
    layer is fixable with nc_auth_update_scopes.
    """
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.write")
    async def fake_tool(ctx: Context):  # noqa: ARG001
        return "ok"

    with (
        _settings(),
        _stored(["notes.read"]),
        _token_scopes("notes.write"),
        pytest.raises(InsufficientScopeError) as exc_info,
    ):
        await fake_tool(ctx=ctx)

    assert "nc_auth_update_scopes" in str(exc_info.value)


async def test_token_denial_message_points_at_the_oidc_client_not_the_stored_grant():
    """A token-layer denial must say nc_auth_update_scopes will NOT help.

    Without the negative instruction an agent retries that tool forever against
    an "unchanged" response. Both levers are named because either can be the
    cause: the client's allowed_scopes, or the user's own consent selection.
    """
    ctx = _make_login_flow_ctx()

    @require_scopes("semantic.read")
    async def fake_tool(ctx: Context):  # noqa: ARG001
        return "ok"

    with (
        _settings(),
        _stored("all"),
        _token_scopes("openid"),
        pytest.raises(InsufficientScopeError) as exc_info,
    ):
        await fake_tool(ctx=ctx)

    msg = str(exc_info.value)
    assert "cannot grant them" in msg
    assert "consent" in msg
    assert "re-authorize" in msg


async def test_offline_access_branch_skipped_under_login_flow():
    """Login Flow v2 must not be routed into the Flow-2 provisioning branch.

    That branch names 'provision_nextcloud_access' (wrong tool here) and raises
    ProvisioningRequiredError, which carries no WWW-Authenticate challenge, so
    a client loses its step-up path. It became reachable for login_flow only
    once NULL stored scopes started falling through.
    """
    ctx = _make_login_flow_ctx()

    @require_scopes("notes.read")
    async def fake_tool(ctx: Context):  # noqa: ARG001
        return "ok"

    with (
        _settings(offline_access=True),
        _stored("all"),
        _token_scopes("openid"),
        pytest.raises(InsufficientScopeError),
    ):
        await fake_tool(ctx=ctx)


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
