"""Enforcement of the ``mail.draft`` scope on the real tool functions.

``test_mail_write_tools.py`` pins the BasicAuth pass-through and checks the
scope *metadata*; these drive the registered tools through ``@require_scopes``
with a verified token in the SDK auth contextvar, the way
``test_require_scopes_enforcement.py`` does for a synthetic tool. The point is
the deployment ``mail.draft`` exists for: a token that may prepare drafts but
not send, so the boundary is checked in both directions.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import LATEST_PROTOCOL_VERSION

from nextcloud_mcp_server.auth.scope_authorization import InsufficientScopeError
from nextcloud_mcp_server.models.mail import SaveDraftResponse
from nextcloud_mcp_server.server.mail import configure_mail_tools

pytestmark = pytest.mark.unit

_RECIPIENTS = '[{"email": "alice@example.com"}]'


def _make_ctx() -> Context:
    return Context(
        request_context=ServerRequestContext(
            session=None,
            lifespan_context=None,
            protocol_version=LATEST_PROTOCOL_VERSION,
            method="tools/call",
            request_id="req-test",
            meta=None,
        )
    )


@contextmanager
def _authenticated_with(scopes: list[str]) -> Iterator[None]:
    """Populate the auth contextvar exactly as AuthContextMiddleware does."""
    token = AccessToken(
        token="opaque-token",
        client_id="test-client",
        scopes=scopes,
        expires_at=None,
        resource="alice",
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


@pytest.fixture(autouse=True)
def oauth_settings():
    """Token-only enforcement: no Login Flow stored-scope layer, no offline access."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(
            enable_login_flow=False, enable_offline_access=False
        ),
    ):
        yield


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer(name="test-mail-draft-scope")
    configure_mail_tools(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


@pytest.fixture
def fake_mail(mocker) -> AsyncMock:
    mail = AsyncMock()
    mail.save_draft.return_value = {
        "draft": {"id": 42},
        "response": {"status": "success", "data": "Message moved to IMAP"},
    }
    client = SimpleNamespace(mail=mail)

    async def fake_get_client(ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.mail.get_client", side_effect=fake_get_client
    )
    return mail


async def test_draft_scope_alone_is_enough_to_save_a_draft(tools, fake_mail):
    with _authenticated_with(["openid", "mail.draft"]):
        result = await tools["nc_mail_save_draft"](
            account_id=1, to=_RECIPIENTS, subject="Hi", body="body", ctx=_make_ctx()
        )

    assert isinstance(result, SaveDraftResponse)
    fake_mail.save_draft.assert_awaited_once()


async def test_send_write_and_read_scopes_do_not_grant_drafts(tools, fake_mail):
    """mail.draft is not implied by any other mail scope."""
    save_draft = tools["nc_mail_save_draft"]
    ctx = _make_ctx()
    with _authenticated_with(["openid", "mail.read", "mail.write", "mail.send"]):
        with pytest.raises(InsufficientScopeError) as exc_info:
            await save_draft(
                account_id=1, to=_RECIPIENTS, subject="Hi", body="body", ctx=ctx
            )

    assert exc_info.value.missing_scopes == ["mail.draft"]
    fake_mail.save_draft.assert_not_awaited()


async def test_draft_scope_does_not_grant_sending(tools, fake_mail):
    """The reason the scope exists: a draft-only token must not send mail."""
    send = tools["nc_mail_send_message"]
    ctx = _make_ctx()
    with _authenticated_with(["openid", "mail.read", "mail.draft"]):
        with pytest.raises(InsufficientScopeError) as exc_info:
            await send(account_id=1, to=_RECIPIENTS, subject="Hi", body="body", ctx=ctx)

    assert exc_info.value.missing_scopes == ["mail.send"]
    fake_mail.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("nc_mail_delete_message", {"message_id": 7}),
        ("nc_mail_move_message", {"message_id": 7, "destination_mailbox_id": 3}),
        ("nc_mail_set_flags", {"message_id": 7, "seen": True}),
    ],
)
async def test_draft_scope_does_not_grant_changing_other_mail(
    tools, fake_mail, tool, kwargs
):
    fn = tools[tool]
    ctx = _make_ctx()
    with _authenticated_with(["openid", "mail.read", "mail.draft"]):
        with pytest.raises(InsufficientScopeError) as exc_info:
            await fn(**kwargs, ctx=ctx)

    assert exc_info.value.missing_scopes == ["mail.write"]
    assert fake_mail.mock_calls == []
