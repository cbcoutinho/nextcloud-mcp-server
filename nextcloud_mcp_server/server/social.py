"""MCP tools for the Nextcloud Social app (Mastodon client API).

Lets an agent with a Nextcloud account take part in the fediverse: read its
timelines and notifications, post and reply, follow, favourite and boost.

Social answers failures as ``{"error": "..."}`` with a status that says what to
do about it (404 gone, 422 refused, 401 credentials), so the translation to
``MCPError`` lives in one context manager that passes the server's own wording
through, rather than a try/except per tool.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from httpx import HTTPStatusError, RequestError, Response
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.social import (
    SocialAccountListResponse,
    SocialAccountResponse,
    SocialContextResponse,
    SocialNotificationListResponse,
    SocialNotificationType,
    SocialRelationshipListResponse,
    SocialRelationshipResponse,
    SocialStatusListResponse,
    SocialStatusResponse,
    SocialTimeline,
    SocialVisibility,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)

#: What a 401 from Social means for a Basic-auth caller. The header half of the
#: usual cause is ruled out -- the client always sends ``OCS-APIRequest`` -- so
#: what is left is the account itself.
_UNAUTHORIZED_HINT = (
    "Nextcloud Social did not accept this user. Social acts through the user's "
    "Social account (actor): if it has not been created, open Social once in "
    "the browser or have an admin run `occ social:account:create <uid>`. Also "
    "check that the Social app is enabled for this user"
)


def _server_error(response: Response) -> str | None:
    """Social's own ``{"error": "..."}`` message, if the body carries one.

    ``None`` means the body was not Social's: an HTML page from Nextcloud (app
    not installed or disabled, so no Social route answered) or an empty body.
    """
    try:
        payload: Any = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return None


@contextmanager
def _social_errors(action: str) -> Iterator[None]:
    """Re-raise a Social failure as the message the model should act on."""
    try:
        yield
    except ValueError as e:
        # The client's input validation, before any request is sent -- or a
        # response Social shaped unexpectedly (pydantic's ValidationError is a
        # ValueError too).
        raise MCPError(code=-1, message=f"Failed {action}: {e}")
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error {action}: {e}")
    except HTTPStatusError as e:
        status = e.response.status_code
        detail = _server_error(e.response)
        if status == 401:
            message = f"{_UNAUTHORIZED_HINT} (server said: {detail or 'unauthorized'})"
        elif detail is None and status == 404:
            message = (
                "Nextcloud answered 404 without a Social error body — is the "
                "Social app installed and enabled for this user?"
            )
        else:
            message = detail or f"server error ({status})"
        raise MCPError(code=-1, message=f"Failed {action}: {message}")


_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(idempotent_hint=False, open_world_hint=True)


# Read tools


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_my_account(ctx: Context) -> SocialAccountResponse:
    """Get your own Social account: id, handle, bio and follower counts."""
    client = await get_client(ctx)
    with _social_errors("reading your Social account"):
        account = await client.social.verify_credentials()
    return SocialAccountResponse(account=account)


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_timeline(
    ctx: Context,
    timeline: SocialTimeline = "home",
    limit: int = 20,
    max_id: str | None = None,
    min_id: str | None = None,
    since_id: str | None = None,
) -> SocialStatusListResponse:
    """Read a timeline, newest first.

    timeline: 'home' (accounts you follow), 'local' (this server's public
    posts) or 'public' (everything this server knows of).
    limit: 1-50. Paging: max_id for older statuses, min_id or since_id for only
    statuses newer than an id. Pass cursors.prev_min_id from an earlier call as
    min_id to poll for what is new.
    """
    client = await get_client(ctx)
    with _social_errors(f"reading the {timeline} timeline"):
        statuses, cursors = await client.social.get_timeline(
            timeline, limit=limit, max_id=max_id, min_id=min_id, since_id=since_id
        )
    return SocialStatusListResponse(
        statuses=statuses, count=len(statuses), cursors=cursors
    )


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_hashtag_timeline(
    ctx: Context,
    hashtag: str,
    local: bool = False,
    limit: int = 20,
    max_id: str | None = None,
    min_id: str | None = None,
    since_id: str | None = None,
) -> SocialStatusListResponse:
    """Read statuses carrying a hashtag, newest first.

    hashtag: with or without '#'. local: only this server's posts. limit and
    paging as for nc_social_get_timeline.
    """
    client = await get_client(ctx)
    with _social_errors(f"reading hashtag {hashtag!r}"):
        statuses, cursors = await client.social.get_hashtag_timeline(
            hashtag,
            local=local,
            limit=limit,
            max_id=max_id,
            min_id=min_id,
            since_id=since_id,
        )
    return SocialStatusListResponse(
        statuses=statuses, count=len(statuses), cursors=cursors
    )


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_status(ctx: Context, status_id: str) -> SocialStatusResponse:
    """Get one status by id."""
    client = await get_client(ctx)
    with _social_errors(f"reading status {status_id}"):
        status = await client.social.get_status(status_id)
    return SocialStatusResponse(status=status)


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_status_context(
    ctx: Context, status_id: str
) -> SocialContextResponse:
    """Get the thread around a status: ancestors (oldest first) and replies."""
    client = await get_client(ctx)
    with _social_errors(f"reading the thread of status {status_id}"):
        context = await client.social.get_status_context(status_id)
    return SocialContextResponse(status_id=status_id, context=context)


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_account_statuses(
    ctx: Context,
    account: str,
    limit: int = 20,
    max_id: str | None = None,
    min_id: str | None = None,
    since_id: str | None = None,
) -> SocialStatusListResponse:
    """Read the statuses one account posted, newest first.

    Other readers get only its public and unlisted statuses, followers included:
    Social serves followers-only posts to their author alone here. A follower
    reads those on the home or hashtag timeline instead.

    account: numeric account id, or a handle ('alice', 'alice@example.org').
    limit and paging as for nc_social_get_timeline.
    """
    client = await get_client(ctx)
    with _social_errors(f"reading statuses of {account!r}"):
        statuses, cursors = await client.social.get_account_statuses(
            account, limit=limit, max_id=max_id, min_id=min_id, since_id=since_id
        )
    return SocialStatusListResponse(
        statuses=statuses, count=len(statuses), cursors=cursors
    )


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_account(ctx: Context, account: str) -> SocialAccountResponse:
    """Get an account by numeric id or handle ('alice', '@alice@example.org').

    A remote handle this server has not seen yet is fetched from its server.
    """
    client = await get_client(ctx)
    with _social_errors(f"reading account {account!r}"):
        found = await client.social.get_account(account)
    return SocialAccountResponse(account=found)


@require_scopes("social.read")
@instrument_tool
async def nc_social_search_accounts(
    ctx: Context,
    query: str,
    limit: int = 40,
    resolve: bool = False,
    following: bool = False,
) -> SocialAccountListResponse:
    """Search accounts by name or handle.

    limit: 1-80. resolve: also look a '@user@host' handle up on its own server.
    following: only accounts you follow.
    """
    client = await get_client(ctx)
    with _social_errors(f"searching accounts for {query!r}"):
        accounts = await client.social.search_accounts(
            query, limit=limit, resolve=resolve, following=following
        )
    return SocialAccountListResponse(accounts=accounts, count=len(accounts))


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_relationships(
    ctx: Context, account_ids: list[str]
) -> SocialRelationshipListResponse:
    """Get your relationship (following, followed_by, requested, ...) with accounts.

    account_ids: numeric account ids. Handles are not accepted here.
    """
    if not account_ids:
        raise MCPError(code=-1, message="No account ids given")
    client = await get_client(ctx)
    with _social_errors("reading relationships"):
        relationships = await client.social.get_relationships(account_ids)
    return SocialRelationshipListResponse(relationships=relationships)


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_followers(
    ctx: Context, account: str, limit: int = 20, max_id: str | None = None
) -> SocialAccountListResponse:
    """List the accounts following an account (id or handle).

    limit: 1-50. max_id: cursors.next_max_id from the previous page.
    """
    client = await get_client(ctx)
    with _social_errors(f"listing followers of {account!r}"):
        accounts, cursors = await client.social.get_followers(
            account, limit=limit, max_id=max_id
        )
    return SocialAccountListResponse(
        accounts=accounts, count=len(accounts), cursors=cursors
    )


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_following(
    ctx: Context, account: str, limit: int = 20, max_id: str | None = None
) -> SocialAccountListResponse:
    """List the accounts an account (id or handle) follows.

    limit: 1-50. max_id: cursors.next_max_id from the previous page.
    """
    client = await get_client(ctx)
    with _social_errors(f"listing accounts followed by {account!r}"):
        accounts, cursors = await client.social.get_following(
            account, limit=limit, max_id=max_id
        )
    return SocialAccountListResponse(
        accounts=accounts, count=len(accounts), cursors=cursors
    )


@require_scopes("social.read")
@instrument_tool
async def nc_social_get_notifications(
    ctx: Context,
    limit: int = 20,
    max_id: str | None = None,
    min_id: str | None = None,
    since_id: str | None = None,
    types: list[SocialNotificationType] | None = None,
    exclude_types: list[SocialNotificationType] | None = None,
) -> SocialNotificationListResponse:
    """Read your notifications (mentions, follows, favourites, boosts, ...).

    types / exclude_types: keep or drop kinds, e.g. ['mention'] for replies and
    mentions only. Reading does not mark anything read. limit and paging as for
    nc_social_get_timeline.
    """
    client = await get_client(ctx)
    with _social_errors("reading notifications"):
        notifications, cursors = await client.social.get_notifications(
            limit=limit,
            max_id=max_id,
            min_id=min_id,
            since_id=since_id,
            types=list(types) if types else None,
            exclude_types=list(exclude_types) if exclude_types else None,
        )
    return SocialNotificationListResponse(
        notifications=notifications, count=len(notifications), cursors=cursors
    )


# Write tools


@require_scopes("social.write")
@instrument_tool
async def nc_social_post_status(
    ctx: Context,
    status: str,
    visibility: SocialVisibility | None = None,
    in_reply_to_id: str | None = None,
    spoiler_text: str | None = None,
    language: str | None = None,
) -> SocialStatusResponse:
    """Publish a status, or reply to one. Mention people by writing @handle.

    visibility: public, unlisted, private (followers only) or direct (only the
    mentioned accounts). Omit it to use the account's default visibility.
    in_reply_to_id: status id to reply to. spoiler_text: content warning shown
    before the text. language: ISO 639 code, e.g. 'en'.
    """
    if not status.strip():
        raise MCPError(code=-1, message="Status text must not be empty")
    client = await get_client(ctx)
    with _social_errors("posting the status"):
        posted = await client.social.post_status(
            status,
            visibility=visibility,
            in_reply_to_id=in_reply_to_id,
            spoiler_text=spoiler_text,
            language=language,
        )
    return SocialStatusResponse(status=posted)


@require_scopes("social.write")
@instrument_tool
async def nc_social_delete_status(ctx: Context, status_id: str) -> SocialStatusResponse:
    """Delete one of your own statuses. Returns the status as it was."""
    client = await get_client(ctx)
    with _social_errors(f"deleting status {status_id}"):
        deleted = await client.social.delete_status(status_id)
    return SocialStatusResponse(status=deleted)


@require_scopes("social.write")
@instrument_tool
async def nc_social_follow_account(
    ctx: Context, account: str
) -> SocialRelationshipResponse:
    """Follow an account (id or handle). A locked account leaves it 'requested'."""
    client = await get_client(ctx)
    with _social_errors(f"following {account!r}"):
        relationship = await client.social.follow(account)
    return SocialRelationshipResponse(relationship=relationship)


@require_scopes("social.write")
@instrument_tool
async def nc_social_unfollow_account(
    ctx: Context, account: str
) -> SocialRelationshipResponse:
    """Unfollow an account (id or handle), or withdraw a pending follow request."""
    client = await get_client(ctx)
    with _social_errors(f"unfollowing {account!r}"):
        relationship = await client.social.unfollow(account)
    return SocialRelationshipResponse(relationship=relationship)


async def _status_action(
    ctx: Context, status_id: str, action: str
) -> SocialStatusResponse:
    client = await get_client(ctx)
    with _social_errors(f"applying {action} to status {status_id}"):
        status = await client.social.status_action(status_id, action)
    return SocialStatusResponse(status=status)


@require_scopes("social.write")
@instrument_tool
async def nc_social_favourite_status(
    ctx: Context, status_id: str
) -> SocialStatusResponse:
    """Favourite (like) a status."""
    return await _status_action(ctx, status_id, "favourite")


@require_scopes("social.write")
@instrument_tool
async def nc_social_unfavourite_status(
    ctx: Context, status_id: str
) -> SocialStatusResponse:
    """Remove your favourite from a status."""
    return await _status_action(ctx, status_id, "unfavourite")


@require_scopes("social.write")
@instrument_tool
async def nc_social_boost_status(ctx: Context, status_id: str) -> SocialStatusResponse:
    """Boost (reblog) a status to your followers. Only public statuses can be boosted."""
    return await _status_action(ctx, status_id, "reblog")


@require_scopes("social.write")
@instrument_tool
async def nc_social_unboost_status(
    ctx: Context, status_id: str
) -> SocialStatusResponse:
    """Undo your boost of a status."""
    return await _status_action(ctx, status_id, "unreblog")


def configure_social_tools(mcp: MCPServer) -> None:
    """Configure Nextcloud Social MCP tools."""
    read_tools = (
        ("Get My Social Account", nc_social_get_my_account),
        ("Get Social Timeline", nc_social_get_timeline),
        ("Get Social Hashtag Timeline", nc_social_get_hashtag_timeline),
        ("Get Social Status", nc_social_get_status),
        ("Get Social Status Thread", nc_social_get_status_context),
        ("Get Social Account Statuses", nc_social_get_account_statuses),
        ("Get Social Account", nc_social_get_account),
        ("Search Social Accounts", nc_social_search_accounts),
        ("Get Social Relationships", nc_social_get_relationships),
        ("List Social Followers", nc_social_get_followers),
        ("List Social Following", nc_social_get_following),
        ("Get Social Notifications", nc_social_get_notifications),
    )
    for title, fn in read_tools:
        mcp.tool(title=title, annotations=_READ)(fn)

    # None of these is marked idempotent: repeating one has not been verified
    # to be a no-op against Social (re-following, for one, bumps the
    # following count again in v0.24.1's ApiController::accountFollow).
    write_tools = (
        ("Post Social Status", nc_social_post_status),
        ("Follow Social Account", nc_social_follow_account),
        ("Unfollow Social Account", nc_social_unfollow_account),
        ("Favourite Social Status", nc_social_favourite_status),
        ("Unfavourite Social Status", nc_social_unfavourite_status),
        ("Boost Social Status", nc_social_boost_status),
        ("Unboost Social Status", nc_social_unboost_status),
    )
    for title, fn in write_tools:
        mcp.tool(title=title, annotations=_WRITE)(fn)

    mcp.tool(
        title="Delete Social Status",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )(nc_social_delete_status)
