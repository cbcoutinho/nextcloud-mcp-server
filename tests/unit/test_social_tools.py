"""Server-layer tests for the Nextcloud Social tools.

The client tests pin the wire; these pin the tool layer: that each tool is
registered with the scope and annotations it claims, forwards its arguments
unchanged (above all, never invents a ``visibility``), wraps results in typed
``BaseResponse`` models, and turns Social's failures into messages an agent can
act on.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.models.base import BaseResponse
from nextcloud_mcp_server.models.social import (
    SocialAccount,
    SocialAccountListResponse,
    SocialAccountResponse,
    SocialContext,
    SocialContextResponse,
    SocialNotification,
    SocialNotificationListResponse,
    SocialPageCursors,
    SocialRelationship,
    SocialRelationshipListResponse,
    SocialRelationshipResponse,
    SocialStatus,
    SocialStatusListResponse,
    SocialStatusResponse,
)
from nextcloud_mcp_server.server import APP_CAPABILITY_KEY, AVAILABLE_APPS
from nextcloud_mcp_server.server.social import configure_social_tools

pytestmark = pytest.mark.unit

ACCOUNT = SocialAccount(id="7", acct="alice", username="alice")
STATUS = SocialStatus(id="101", text="hi")
RELATIONSHIP = SocialRelationship(id="8", following=True)
CURSORS = SocialPageCursors(next_max_id="90", prev_min_id="101")

READ_TOOLS = {
    "nc_social_get_my_account",
    "nc_social_get_timeline",
    "nc_social_get_hashtag_timeline",
    "nc_social_get_status",
    "nc_social_get_status_context",
    "nc_social_get_account_statuses",
    "nc_social_get_account",
    "nc_social_search_accounts",
    "nc_social_get_relationships",
    "nc_social_get_followers",
    "nc_social_get_following",
    "nc_social_get_notifications",
}
WRITE_TOOLS = {
    "nc_social_post_status",
    "nc_social_delete_status",
    "nc_social_follow_account",
    "nc_social_unfollow_account",
    "nc_social_favourite_status",
    "nc_social_unfavourite_status",
    "nc_social_boost_status",
    "nc_social_unboost_status",
}


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Pin ``require_scopes`` to the BasicAuth pass-through path.

    These call tool functions with no transport and so no verified token,
    which the decorator correctly denies under any OAuth-style mode.
    """
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def social_tools() -> dict:
    """Register the Social tools on a fresh MCPServer and return them by name."""
    mcp = MCPServer(name="test-social-tools")
    configure_social_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


@pytest.fixture
def fake_social(mocker):
    """Install a mock client and hand back its ``social`` namespace."""
    social = AsyncMock()
    client = SimpleNamespace(social=social)

    async def fake_get_client(ctx):
        return client

    mocker.patch(
        "nextcloud_mcp_server.server.social.get_client", side_effect=fake_get_client
    )
    return social


def _ctx() -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.request_context = SimpleNamespace()
    return ctx


def _http_error(status: int, body: dict | None = None, text: str = "") -> Exception:
    request = httpx.Request("GET", "http://test.local/index.php/apps/social/api/v1/x")
    if body is not None:
        response = httpx.Response(status, json=body, request=request)
    else:
        response = httpx.Response(status, text=text, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# Registration


def test_social_is_a_registered_app():
    assert AVAILABLE_APPS["social"] is configure_social_tools


def test_social_is_not_capability_gated():
    """Social publishes no capability block; gating would hide working tools."""
    assert "social" not in APP_CAPABILITY_KEY


def test_every_tool_is_registered(social_tools):
    assert set(social_tools) == READ_TOOLS | WRITE_TOOLS


def test_read_tools_need_read_scope_and_are_read_only(social_tools):
    for name in READ_TOOLS:
        tool = social_tools[name]
        assert tool.fn._required_scopes == ["social.read"], name
        assert tool.annotations.read_only_hint is True, name
        assert tool.annotations.open_world_hint is True, name


def test_write_tools_need_write_scope_and_are_not_read_only(social_tools):
    for name in WRITE_TOOLS:
        tool = social_tools[name]
        assert tool.fn._required_scopes == ["social.write"], name
        assert tool.annotations.read_only_hint is not True, name
        assert tool.annotations.open_world_hint is True, name


def test_delete_is_destructive_and_idempotent(social_tools):
    annotations = social_tools["nc_social_delete_status"].annotations
    assert annotations.destructive_hint is True
    assert annotations.idempotent_hint is True


def test_every_tool_has_a_title(social_tools):
    for name, tool in social_tools.items():
        assert tool.title and tool.title != name, name


# Read tools


async def test_get_my_account(social_tools, fake_social):
    fake_social.verify_credentials.return_value = ACCOUNT

    result = await social_tools["nc_social_get_my_account"].fn(_ctx())

    assert isinstance(result, SocialAccountResponse)
    assert result.account.acct == "alice"


async def test_get_timeline_forwards_timeline_and_cursors(social_tools, fake_social):
    fake_social.get_timeline.return_value = ([STATUS], CURSORS)

    result = await social_tools["nc_social_get_timeline"].fn(
        _ctx(), timeline="local", limit=5, min_id="100"
    )

    assert isinstance(result, SocialStatusListResponse)
    assert (result.count, result.cursors.prev_min_id) == (1, "101")
    fake_social.get_timeline.assert_awaited_once_with(
        "local", limit=5, max_id=None, min_id="100", since_id=None
    )


async def test_get_timeline_defaults_to_home(social_tools, fake_social):
    fake_social.get_timeline.return_value = ([], SocialPageCursors())

    await social_tools["nc_social_get_timeline"].fn(_ctx())

    assert fake_social.get_timeline.await_args.args == ("home",)


async def test_get_hashtag_timeline(social_tools, fake_social):
    fake_social.get_hashtag_timeline.return_value = ([STATUS], CURSORS)

    result = await social_tools["nc_social_get_hashtag_timeline"].fn(
        _ctx(), hashtag="#mcp", local=True, since_id="5"
    )

    assert isinstance(result, SocialStatusListResponse)
    fake_social.get_hashtag_timeline.assert_awaited_once_with(
        "#mcp", local=True, limit=20, max_id=None, min_id=None, since_id="5"
    )


async def test_get_status_and_context(social_tools, fake_social):
    fake_social.get_status.return_value = STATUS
    fake_social.get_status_context.return_value = SocialContext(descendants=[STATUS])

    status = await social_tools["nc_social_get_status"].fn(_ctx(), status_id="101")
    context = await social_tools["nc_social_get_status_context"].fn(
        _ctx(), status_id="101"
    )

    assert isinstance(status, SocialStatusResponse)
    assert isinstance(context, SocialContextResponse)
    assert context.status_id == "101"
    assert context.context.descendants[0].id == "101"


async def test_get_account_statuses(social_tools, fake_social):
    fake_social.get_account_statuses.return_value = ([STATUS], CURSORS)

    result = await social_tools["nc_social_get_account_statuses"].fn(
        _ctx(), account="alice@example.org", max_id="90"
    )

    assert isinstance(result, SocialStatusListResponse)
    fake_social.get_account_statuses.assert_awaited_once_with(
        "alice@example.org", limit=20, max_id="90", min_id=None, since_id=None
    )


async def test_get_and_search_accounts(social_tools, fake_social):
    fake_social.get_account.return_value = ACCOUNT
    fake_social.search_accounts.return_value = [ACCOUNT]

    found = await social_tools["nc_social_get_account"].fn(_ctx(), account="alice")
    searched = await social_tools["nc_social_search_accounts"].fn(
        _ctx(), query="ali", resolve=True
    )

    assert isinstance(found, SocialAccountResponse)
    assert isinstance(searched, SocialAccountListResponse)
    assert searched.count == 1
    assert searched.cursors is None
    fake_social.search_accounts.assert_awaited_once_with(
        "ali", limit=40, resolve=True, following=False
    )


async def test_get_relationships(social_tools, fake_social):
    fake_social.get_relationships.return_value = [RELATIONSHIP]

    result = await social_tools["nc_social_get_relationships"].fn(
        _ctx(), account_ids=["8"]
    )

    assert isinstance(result, SocialRelationshipListResponse)
    assert result.relationships[0].following is True


async def test_get_relationships_without_ids_is_an_error(social_tools, fake_social):
    """An empty id list would be an empty answer reported as success."""
    get_relationships = social_tools["nc_social_get_relationships"].fn
    ctx = _ctx()
    with pytest.raises(MCPError, match="No account ids"):
        await get_relationships(ctx, account_ids=[])

    fake_social.get_relationships.assert_not_awaited()


@pytest.mark.parametrize(
    ("tool", "method"),
    [
        ("nc_social_get_followers", "get_followers"),
        ("nc_social_get_following", "get_following"),
    ],
)
async def test_follower_lists_page(social_tools, fake_social, tool, method):
    getattr(fake_social, method).return_value = ([ACCOUNT], CURSORS)

    result = await social_tools[tool].fn(_ctx(), account="7", max_id="30")

    assert isinstance(result, SocialAccountListResponse)
    assert result.cursors.next_max_id == "90"
    getattr(fake_social, method).assert_awaited_once_with("7", limit=20, max_id="30")


async def test_get_notifications_forwards_type_filters(social_tools, fake_social):
    fake_social.get_notifications.return_value = (
        [SocialNotification(id="55", type="mention", status=STATUS)],
        CURSORS,
    )

    result = await social_tools["nc_social_get_notifications"].fn(
        _ctx(), types=["mention"], exclude_types=None, since_id="50"
    )

    assert isinstance(result, SocialNotificationListResponse)
    assert result.notifications[0].type == "mention"
    fake_social.get_notifications.assert_awaited_once_with(
        limit=20,
        max_id=None,
        min_id=None,
        since_id="50",
        types=["mention"],
        exclude_types=None,
    )


# Write tools


async def test_post_status_does_not_invent_a_visibility(social_tools, fake_social):
    """Omitted visibility must reach the client as None: the account default wins."""
    fake_social.post_status.return_value = STATUS

    result = await social_tools["nc_social_post_status"].fn(_ctx(), status="hi @bob")

    assert isinstance(result, SocialStatusResponse)
    fake_social.post_status.assert_awaited_once_with(
        "hi @bob",
        visibility=None,
        in_reply_to_id=None,
        spoiler_text=None,
        language=None,
    )


async def test_post_status_forwards_a_reply(social_tools, fake_social):
    fake_social.post_status.return_value = STATUS

    await social_tools["nc_social_post_status"].fn(
        _ctx(), status="yes", visibility="direct", in_reply_to_id="100"
    )

    kwargs = fake_social.post_status.await_args.kwargs
    assert (kwargs["visibility"], kwargs["in_reply_to_id"]) == ("direct", "100")


async def test_post_status_rejects_blank_text(social_tools, fake_social):
    post_status = social_tools["nc_social_post_status"].fn
    ctx = _ctx()
    with pytest.raises(MCPError, match="must not be empty"):
        await post_status(ctx, status="   ")

    fake_social.post_status.assert_not_awaited()


async def test_delete_status(social_tools, fake_social):
    fake_social.delete_status.return_value = STATUS

    result = await social_tools["nc_social_delete_status"].fn(_ctx(), status_id="101")

    assert isinstance(result, SocialStatusResponse)
    fake_social.delete_status.assert_awaited_once_with("101")


@pytest.mark.parametrize(
    ("tool", "method"),
    [
        ("nc_social_follow_account", "follow"),
        ("nc_social_unfollow_account", "unfollow"),
    ],
)
async def test_follow_tools(social_tools, fake_social, tool, method):
    getattr(fake_social, method).return_value = RELATIONSHIP

    result = await social_tools[tool].fn(_ctx(), account="bob@example.org")

    assert isinstance(result, SocialRelationshipResponse)
    getattr(fake_social, method).assert_awaited_once_with("bob@example.org")


@pytest.mark.parametrize(
    ("tool", "action"),
    [
        ("nc_social_favourite_status", "favourite"),
        ("nc_social_unfavourite_status", "unfavourite"),
        ("nc_social_boost_status", "reblog"),
        ("nc_social_unboost_status", "unreblog"),
    ],
)
async def test_status_action_tools_map_to_socials_verbs(
    social_tools, fake_social, tool, action
):
    fake_social.status_action.return_value = STATUS

    result = await social_tools[tool].fn(_ctx(), status_id="101")

    assert isinstance(result, SocialStatusResponse)
    fake_social.status_action.assert_awaited_once_with("101", action)


def test_every_tool_returns_a_base_response(social_tools):
    """MCPServer mangles raw lists; every tool must return a BaseResponse."""
    for name, tool in social_tools.items():
        annotation = tool.fn.__annotations__["return"]
        assert issubclass(annotation, BaseResponse), name


# Error translation


async def test_401_names_the_social_account_fix(social_tools, fake_social):
    fake_social.verify_credentials.side_effect = _http_error(
        401, {"error": "the access_token was revoked"}
    )
    get_my_account = social_tools["nc_social_get_my_account"].fn
    ctx = _ctx()
    with pytest.raises(MCPError) as exc:
        await get_my_account(ctx)

    message = str(exc.value)
    assert "occ social:account:create" in message
    assert "the access_token was revoked" in message


async def test_socials_own_error_message_is_passed_through(social_tools, fake_social):
    fake_social.post_status.side_effect = _http_error(
        422, {"error": "unknown visibility: everyone"}
    )
    post_status = social_tools["nc_social_post_status"].fn
    ctx = _ctx()
    with pytest.raises(MCPError, match="unknown visibility: everyone"):
        await post_status(ctx, status="hi")


async def test_404_without_a_social_body_suggests_the_app_is_missing(
    social_tools, fake_social
):
    fake_social.get_status.side_effect = _http_error(404, text="<html>Not found</html>")
    get_status = social_tools["nc_social_get_status"].fn
    ctx = _ctx()
    with pytest.raises(MCPError, match="Social app installed"):
        await get_status(ctx, status_id="1")


async def test_404_with_a_social_body_is_a_plain_not_found(social_tools, fake_social):
    fake_social.get_status.side_effect = _http_error(404, {"error": "not found"})
    get_status = social_tools["nc_social_get_status"].fn
    ctx = _ctx()
    with pytest.raises(MCPError) as exc:
        await get_status(ctx, status_id="1")

    assert "not found" in str(exc.value)
    assert "installed" not in str(exc.value)


async def test_input_validation_becomes_a_tool_error(social_tools, fake_social):
    fake_social.get_status.side_effect = ValueError("Invalid status id 'x'")
    get_status = social_tools["nc_social_get_status"].fn
    ctx = _ctx()
    with pytest.raises(MCPError, match="Invalid status id"):
        await get_status(ctx, status_id="x")


async def test_network_error_is_reported_as_such(social_tools, fake_social):
    request = httpx.Request("GET", "http://test.local/")
    fake_social.get_timeline.side_effect = httpx.ConnectError("down", request=request)
    get_timeline = social_tools["nc_social_get_timeline"].fn
    ctx = _ctx()
    with pytest.raises(MCPError, match="Network error"):
        await get_timeline(ctx)
