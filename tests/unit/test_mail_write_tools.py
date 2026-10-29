"""Server-layer tests for the Mail write tools (GH #1148 and friends).

These register the Mail tools on a fresh ``MCPServer`` and invoke each tool's
underlying function directly, mirroring ``test_webdav_tools_exclusion.py``.
The point is the wiring the client-layer tests can't see: which flags a tool
forwards, that a tag name is resolved to the server's own ``imapLabel`` before
being used in a URL, and that the tools return typed ``BaseResponse`` models.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.models.auth import ALL_SUPPORTED_SCOPES
from nextcloud_mcp_server.models.mail import (
    MailActionResponse,
    MailTagResponse,
    SaveDraftResponse,
)
from nextcloud_mcp_server.server import AVAILABLE_APPS
from nextcloud_mcp_server.server.mail import configure_mail_tools
from nextcloud_mcp_server.server.semantic import configure_semantic_tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path.

    Same rationale as the WebDAV tool tests: these call tool functions with no
    transport and so no verified token, and the decorator correctly denies that
    under any OAuth-style mode.
    """
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def mail_tools() -> dict:
    """Register the Mail tools on a fresh MCPServer and return them by name."""
    mcp = MCPServer(name="test-mail-tools")
    configure_mail_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


@pytest.fixture
def fake_mail(mocker):
    """Install a mock mail client and hand back its ``mail`` namespace."""
    mail = AsyncMock()
    client = SimpleNamespace(mail=mail)

    async def fake_get_client(ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.mail.get_client", side_effect=fake_get_client
    )
    return mail


def _ctx() -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.request_context = SimpleNamespace()
    return ctx


async def test_set_flags_forwards_only_the_flags_given(mail_tools, fake_mail):
    """Omitted flags must not be sent — sending them would clobber their state."""
    result = await mail_tools["nc_mail_set_flags"].fn(42, _ctx(), seen=True)

    assert isinstance(result, MailActionResponse)
    assert result.message_id == 42
    fake_mail.set_flags.assert_awaited_once_with(42, {"seen": True})


async def test_set_flags_marks_unread(mail_tools, fake_mail):
    """GH #1148's other direction: seen=False puts a message back to unread."""
    await mail_tools["nc_mail_set_flags"].fn(42, _ctx(), seen=False)

    fake_mail.set_flags.assert_awaited_once_with(42, {"seen": False})


async def test_set_flags_maps_junk_to_the_imap_keyword(mail_tools, fake_mail):
    """``junk`` is a custom IMAP keyword, spelled ``$junk`` on the wire."""
    await mail_tools["nc_mail_set_flags"].fn(
        42, _ctx(), flagged=True, answered=False, junk=True
    )

    fake_mail.set_flags.assert_awaited_once_with(
        42, {"flagged": True, "answered": False, "$junk": True}
    )


async def test_set_flags_without_any_flag_is_an_error(mail_tools, fake_mail):
    """A no-flag call would be a silent no-op reported as success."""
    # Setup hoisted out of the with-block so only the call under test can raise
    # (python:S5778).
    set_flags = mail_tools["nc_mail_set_flags"].fn
    ctx = _ctx()
    with pytest.raises(MCPError):
        await set_flags(42, ctx)

    fake_mail.set_flags.assert_not_awaited()


async def test_set_tag_resolves_the_label_server_side(mail_tools, fake_mail):
    """The tag route addresses tags by ``imapLabel``, which the server derives.

    Re-deriving it client-side would mean reimplementing the Mail app's
    modified-UTF-7 encoding; instead the create-or-get response supplies it.
    """
    fake_mail.ensure_tag.return_value = {
        "id": 7,
        "displayName": "AI Index",
        "imapLabel": "$ai_index",
    }

    result = await mail_tools["nc_mail_set_tag"].fn(42, "AI Index", _ctx())

    assert isinstance(result, MailTagResponse)
    assert result.tag.id == 7
    assert result.tag.imap_label == "$ai_index"
    assert result.message_id == 42
    fake_mail.ensure_tag.assert_awaited_once_with("AI Index")
    fake_mail.set_tag.assert_awaited_once_with(42, "$ai_index")


async def test_remove_tag_resolves_the_same_way(mail_tools, fake_mail):
    fake_mail.ensure_tag.return_value = {"id": 7, "imapLabel": "$ai_index"}

    await mail_tools["nc_mail_remove_tag"].fn(42, "AI Index", _ctx())

    fake_mail.remove_tag.assert_awaited_once_with(42, "$ai_index")


async def test_create_tag_returns_the_id_used_by_the_tags_filter(mail_tools, fake_mail):
    """The tag id is the only way to build a ``tags:<id>`` listing filter."""
    fake_mail.ensure_tag.return_value = {
        "id": 7,
        "displayName": "AI Index",
        "imapLabel": "$ai_index",
        "color": "#0082c9",
    }

    result = await mail_tools["nc_mail_create_tag"].fn("AI Index", _ctx())

    assert result.tag.id == 7
    assert result.message_id is None


async def test_move_and_delete_return_typed_responses(mail_tools, fake_mail):
    moved = await mail_tools["nc_mail_move_message"].fn(42, 9, _ctx())
    deleted = await mail_tools["nc_mail_delete_message"].fn(42, _ctx())

    assert isinstance(moved, MailActionResponse)
    assert isinstance(deleted, MailActionResponse)
    fake_mail.move_message.assert_awaited_once_with(42, 9)
    fake_mail.delete_message.assert_awaited_once_with(42)


async def test_get_message_source_returns_raw_text(mail_tools, fake_mail):
    fake_mail.get_message_raw.return_value = "From: a@b.com\r\nSubject: hi\r\n\r\nbody"

    result = await mail_tools["nc_mail_get_message_source"].fn(42, _ctx())

    assert result.message_id == 42
    assert result.source.startswith("From: a@b.com")


def test_write_tools_are_registered_with_the_write_scope(mail_tools):
    """A write tool that slipped through with mail.read would be over-granted."""
    write_tools = [
        "nc_mail_set_flags",
        "nc_mail_create_tag",
        "nc_mail_set_tag",
        "nc_mail_remove_tag",
        "nc_mail_move_message",
        "nc_mail_delete_message",
    ]
    for name in write_tools:
        assert list(mail_tools[name].fn._required_scopes) == ["mail.write"], name


def test_every_tool_scope_is_grantable():
    """Every scope a tool requires must be in ALL_SUPPORTED_SCOPES.

    A scope missing from that set cannot be granted through the provisioning
    path, so the tool is unreachable in OAuth mode while still working under
    BasicAuth (where ``require_scopes`` short-circuits) — which is how
    ``mail.send`` stayed broken unnoticed. This walks every registered app so
    the next omission fails here rather than in a deployment.

    ``configure_semantic_tools`` is registered explicitly because
    ``AVAILABLE_APPS`` deliberately excludes it (semantic search is a
    cross-app feature gated by VECTOR_SYNC_ENABLED, see server/__init__.py).
    That exclusion was the hole this test had: ``semantic.read`` shipped
    ungrantable and undetected (GH #1277).
    """
    mcp = MCPServer(name="test-all-tools")
    for configure in (*AVAILABLE_APPS.values(), configure_semantic_tools):
        configure(mcp)

    required = {
        scope
        for tool in mcp._tool_manager.list_tools()
        for scope in getattr(tool.fn, "_required_scopes", ())
    }
    assert required <= ALL_SUPPORTED_SCOPES, (
        f"scopes used by tools but not grantable: {sorted(required - ALL_SUPPORTED_SCOPES)}"
    )


# --- Drafts -----------------------------------------------------------------


def _saved_draft(draft_id: int = 42) -> dict:
    """What ``MailClient.save_draft`` returns after a successful create + move."""
    return {
        "draft": {"id": draft_id, "accountId": 1, "subject": "Re: Hi"},
        "response": {"status": "success", "data": "Message moved to IMAP"},
    }


async def test_save_draft_parses_recipients_and_returns_a_typed_response(
    mail_tools, fake_mail
):
    """JSON recipients reach the client as dicts, and nothing reaches send."""
    fake_mail.save_draft.return_value = _saved_draft()

    result = await mail_tools["nc_mail_save_draft"].fn(
        1,
        '[{"label": "Alice", "email": "alice@example.com"}]',
        "Re: Hi",
        "Thanks!",
        _ctx(),
        cc='[{"email": "bob@example.com"}]',
    )

    assert isinstance(result, SaveDraftResponse)
    assert result.success is True
    assert result.account_id == 1
    assert result.subject == "Re: Hi"
    assert result.move_result == "Message moved to IMAP"
    assert "Nothing was sent" in result.message
    # The staging id is stale once the draft is on IMAP, so it is not returned.
    assert "draft_id" not in result.model_dump()
    fake_mail.save_draft.assert_awaited_once_with(
        account_id=1,
        to=[{"email": "alice@example.com", "label": "Alice"}],
        subject="Re: Hi",
        body="Thanks!",
        is_html=False,
        cc=[{"email": "bob@example.com"}],
        bcc=None,
    )
    fake_mail.send_message.assert_not_awaited()


def test_save_draft_has_no_ai_marker_parameter(mail_tools):
    """Every draft is marked as AI-generated, so the agent gets no knob for it."""
    params = mail_tools["nc_mail_save_draft"].parameters["properties"]
    assert not any("ai" in name.split("_") for name in params)


async def test_save_draft_accepts_a_draft_without_recipients(mail_tools, fake_mail):
    """Unlike sending, a draft may be saved before its recipients are known."""
    fake_mail.save_draft.return_value = _saved_draft()

    await mail_tools["nc_mail_save_draft"].fn(1, "[]", "Hi", "body", _ctx())

    assert fake_mail.save_draft.await_args.kwargs["to"] == []


@pytest.mark.parametrize(
    ("to", "expected"),
    [
        ("not json", "Invalid JSON in to"),
        ('{"email": "a@b.com"}', "to must be a JSON array"),
        ('[{"label": "no address"}]', "needs an 'email' string"),
        ('["a@b.com"]', "needs an 'email' string"),
    ],
)
async def test_save_draft_rejects_malformed_recipients_before_any_request(
    mail_tools, fake_mail, to, expected
):
    """A bad recipient list fails with a message naming it, before any request."""
    save_draft = mail_tools["nc_mail_save_draft"].fn
    ctx = _ctx()
    with pytest.raises(MCPError) as exc_info:
        await save_draft(1, to, "Hi", "body", ctx)

    assert expected in str(exc_info.value)
    fake_mail.save_draft.assert_not_awaited()


async def test_save_draft_failure_says_nothing_was_sent(mail_tools, fake_mail):
    """An agent must not read a failed save as "maybe it went out"."""
    request = httpx.Request(
        "POST", "http://test.local/index.php/apps/mail/api/drafts/move/42"
    )
    response = httpx.Response(
        500,
        request=request,
        text='{"status":"error","message":"Could not save draft message"}',
    )
    fake_mail.save_draft.side_effect = httpx.HTTPStatusError(
        "HTTP 500", request=request, response=response
    )
    save_draft = mail_tools["nc_mail_save_draft"].fn
    ctx = _ctx()
    with pytest.raises(MCPError) as exc_info:
        await save_draft(1, "[]", "Hi", "body", ctx)

    message = str(exc_info.value)
    assert "500" in message
    assert "Could not save draft message" in message
    assert "Nothing was sent" in message


def test_save_draft_requires_only_the_draft_scope(mail_tools):
    """mail.draft stands alone: neither mail.send nor mail.write may be required.

    Requiring either would make the tool unusable on exactly the deployments it
    is for, the ones that withhold mail.send.
    """
    assert list(mail_tools["nc_mail_save_draft"].fn._required_scopes) == ["mail.draft"]


def test_no_other_mail_tool_accepts_the_draft_scope(mail_tools):
    """mail.draft must not open up sending, moving or deleting mail."""
    for name, tool in mail_tools.items():
        if name != "nc_mail_save_draft":
            assert "mail.draft" not in tool.fn._required_scopes, name


def test_save_draft_annotations_and_description(mail_tools):
    """ADR-017 create semantics; destructive_hint must be explicitly false,
    since MCP defaults it to true for a tool that is not read-only."""
    tool = mail_tools["nc_mail_save_draft"]

    assert tool.title == "Save Mail Draft"
    assert tool.annotations.read_only_hint is not True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is True
    assert "WITHOUT sending" in tool.description
