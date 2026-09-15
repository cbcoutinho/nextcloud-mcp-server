"""Regression tests: @require_scopes must actually deny insufficient scopes.

These tests build a **real** ``RequestContext`` and populate the **real** SDK
auth contextvar the way ``AuthContextMiddleware`` does on an authenticated
request. That matters: the decorator previously read the token from
``ctx.request_context.access_token``, an attribute ``RequestContext`` does not
have, so every call took the "BasicAuth mode — allow" branch and no scope was
ever enforced. Tests that fabricate the attribute (``SimpleNamespace(
access_token=...)``) pass against that broken contract, which is why the gap
survived; assert against the real mechanism instead.

The pre-existing scope tests exercise ``list_tools`` filtering, which is
advisory only — a client can call ``tools/call`` with a name it was never
shown. These cover the enforcement path.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context
from mcp.types import LATEST_PROTOCOL_VERSION

from nextcloud_mcp_server.auth.scope_authorization import (
    InsufficientScopeError,
    ProvisioningRequiredError,
    check_scopes,
    require_scopes,
)


def _request_context() -> ServerRequestContext:
    return ServerRequestContext(
        session=None,
        lifespan_context=None,
        protocol_version=LATEST_PROTOCOL_VERSION,
        method="tools/call",
        request_id="req-test",
        meta=None,
    )


def _make_ctx() -> Context:
    """A real ServerRequestContext — not a mock shaped like one."""
    return Context(request_context=_request_context())


@contextmanager
def _authenticated_with(scopes: list[str]):
    """Populate the auth contextvar exactly as AuthContextMiddleware does."""
    token = AccessToken(
        token="opaque-token",
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
def _settings(*, login_flow: bool = False, offline_access: bool = False):
    fake = SimpleNamespace(
        enable_login_flow=login_flow,
        enable_offline_access=offline_access,
    )
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=fake,
    ):
        yield


@require_scopes("notes.write")
async def _write_tool(ctx: Context) -> str:  # noqa: ARG001
    return "executed"


@pytest.mark.unit
async def test_request_context_has_no_access_token_attribute():
    """Pin the SDK contract that the original bug assumed away.

    If a future refactor reintroduces ``ctx.request_context.access_token`` as
    the token source, this fails and points at why that is wrong.
    """
    assert not hasattr(_request_context(), "access_token")


@pytest.mark.unit
async def test_denies_when_token_lacks_required_scope():
    """The core regression: a token without notes.write must not run the tool."""
    with _settings(), _authenticated_with(["openid"]):
        with pytest.raises(InsufficientScopeError) as exc_info:
            await _write_tool(ctx=_make_ctx())

    assert "notes.write" in exc_info.value.missing_scopes


@pytest.mark.unit
async def test_allows_when_token_carries_required_scope():
    with _settings(), _authenticated_with(["openid", "notes.write"]):
        assert await _write_tool(ctx=_make_ctx()) == "executed"


@pytest.mark.unit
async def test_basicauth_mode_without_token_still_allowed():
    """No OAuth token + no OAuth mode = BasicAuth; Nextcloud enforces its ACLs."""
    with _settings(login_flow=False):
        assert await _write_tool(ctx=_make_ctx()) == "executed"


@pytest.mark.unit
async def test_oauth_mode_without_token_fails_closed():
    """A missing token under OAuth means auth middleware did not run: deny.

    Inferring "BasicAuth" from a missing token is what let the original bug
    grant every scope silently.
    """
    with _settings(login_flow=True):
        with pytest.raises(InsufficientScopeError):
            await _write_tool(ctx=_make_ctx())


@pytest.mark.unit
async def test_offline_access_requires_provisioning_for_nextcloud_scopes():
    """Offline-access mode: a token with no Nextcloud scopes must be told to provision.

    This branch was unreachable while the decorator read the token from
    ``request_context``, so it ships newly-live with this fix and needs its own
    coverage. A token holding only OIDC scopes has completed Flow 1 but not the
    Flow 2 provisioning that grants Nextcloud resource access.
    """
    with _settings(offline_access=True), _authenticated_with(["openid"]):
        with pytest.raises(ProvisioningRequiredError):
            await _write_tool(ctx=_make_ctx())


@pytest.mark.unit
async def test_offline_access_allows_token_carrying_nextcloud_scopes():
    """The same mode must let a provisioned (Flow 2) token through."""
    with _settings(offline_access=True), _authenticated_with(["notes.write"]):
        assert await _write_tool(ctx=_make_ctx()) == "executed"


# ---------------------------------------------------------------------------
# check_scopes — the same latent bug lived here, keyed on an always-None
# getattr. It has no call sites today, so these are its only guard.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_check_scopes_reports_missing_scope():
    with _settings(), _authenticated_with(["notes.read"]):
        has_all, missing = check_scopes(_make_ctx(), "notes.write")

    assert has_all is False
    assert missing == {"notes.write"}


@pytest.mark.unit
async def test_check_scopes_passes_when_covered():
    with _settings(), _authenticated_with(["notes.read", "notes.write"]):
        has_all, missing = check_scopes(_make_ctx(), "notes.write")

    assert has_all is True
    assert missing == set()


@pytest.mark.unit
async def test_check_scopes_denies_verified_token_carrying_no_scopes():
    """A verified token with an empty scope set is not BasicAuth.

    The old guard was ``not token_scopes and getattr(...) is None``; because the
    getattr always returned None it collapsed to "no scopes → allow", waving
    through a real OAuth token that legitimately carried none.
    """
    with _settings(), _authenticated_with([]):
        has_all, missing = check_scopes(_make_ctx(), "notes.write")

    assert has_all is False
    assert missing == {"notes.write"}


@pytest.mark.unit
async def test_check_scopes_allows_basicauth_without_token():
    with _settings():
        has_all, missing = check_scopes(_make_ctx(), "notes.write")

    assert has_all is True
    assert missing == set()


@pytest.mark.unit
async def test_check_scopes_oauth_mode_without_token_fails_closed():
    """Mirrors ``test_oauth_mode_without_token_fails_closed`` for check_scopes.

    Without the mode gate this returned "all granted" under OAuth — the same
    fail-open the decorator was fixed for, left latent for the first caller to
    wire it up.
    """
    with _settings(login_flow=True):
        has_all, missing = check_scopes(_make_ctx(), "notes.write")

    assert has_all is False
    assert missing == {"notes.write"}
