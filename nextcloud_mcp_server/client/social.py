"""HTTP client for the Nextcloud Social app (Mastodon client API).

Social implements the Mastodon client API under its app prefix,
``/apps/social/api/v1/...``; ``_make_request`` adds the ``/index.php`` entry
point. Routes, parameters and response shapes follow Social v0.24.1's
``lib/Controller/ApiController.php`` and ``docs/API.md``.

**Every request carries ``OCS-APIRequest: true``.** These are ordinary app
routes, not OCS ones, but Social resolves a Basic-auth (app password) caller
through the Nextcloud session only when ``IRequest::passesCSRFCheck()`` holds,
and for a non-browser client that header is what makes it hold. Without it
every call that needs a viewer answers ``401 {"error": "the access_token was
revoked"}`` -- and the public timeline silently answers as an anonymous reader.

Paging follows Mastodon: ``max_id`` / ``min_id`` / ``since_id`` cursors, and a
``Link`` header naming the next (older) and previous (newer) page.
"""

import logging
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from httpx import Response

from nextcloud_mcp_server.models.social import (
    SocialAccount,
    SocialContext,
    SocialNotification,
    SocialPageCursors,
    SocialRelationship,
    SocialStatus,
    SocialTimeline,
    SocialVisibility,
)

from .base import BaseNextcloudClient
from .ocs import OCS_REQUEST_HEADERS

logger = logging.getLogger(__name__)

#: Server-side cap on timeline and notification pages
#: (``ProbeOptions::MAX_LIMIT``). Clamped here too so the page an agent asked
#: for and the page it got cannot disagree.
MAX_PAGE_LIMIT = 50

#: Cap on ``/accounts/search`` (Social clamps to 80).
MAX_SEARCH_LIMIT = 80

# Status, account and notification ids are Social's numeric ``nid``s; the
# status routes declare ``int $nid``, and paging cursors are the same numbers.
_NUMERIC_ID_RE = re.compile(r"[0-9]+")

# An account reference as a path segment: a numeric id, or a handle -- ``user``,
# ``@user`` or ``user@host`` (host may carry a port). The first character of
# each part is alphanumeric or ``_``, which keeps ``.``/``..`` out of the path.
# Social also accepts an actor URL here; that form is not offered, because a
# URL is not a path segment.
_ACCOUNT_RE = re.compile(r"@?[A-Za-z0-9_][A-Za-z0-9_.-]*(@[A-Za-z0-9][A-Za-z0-9.:-]*)?")

# A hashtag as a path segment. Hashtags are Unicode words, so this is a
# blocklist of what cannot sit in a path segment rather than an allowlist.
_HASHTAG_FORBIDDEN_RE = re.compile(r"[\s/?#%\\]")


def _validate_numeric_id(value: str, what: str) -> str:
    if not _NUMERIC_ID_RE.fullmatch(value) or int(value) < 1:
        raise ValueError(f"Invalid {what} {value!r}: expected a positive number")
    return value


def validate_status_id(status_id: str) -> str:
    """Return a status id fit for a URL path, or raise ``ValueError``."""
    return _validate_numeric_id(status_id, "status id")


def validate_account(account: str) -> str:
    """Return an account reference fit for a URL path, or raise ``ValueError``."""
    account = account.strip()
    if not _ACCOUNT_RE.fullmatch(account):
        raise ValueError(
            f"Invalid account {account!r}: expected a numeric account id or a "
            "handle such as 'alice' or 'alice@example.org'"
        )
    return account


def validate_hashtag(hashtag: str) -> str:
    """Strip a leading ``#`` and reject what cannot be a path segment."""
    tag = hashtag.strip().removeprefix("#")
    if not tag or tag in (".", "..") or _HASHTAG_FORBIDDEN_RE.search(tag):
        raise ValueError(f"Invalid hashtag {hashtag!r}")
    return tag


def _cursor(link: dict[str, str] | None, param: str) -> str | None:
    """Pull one query parameter out of a parsed ``Link`` header entry."""
    if not link or "url" not in link:
        return None
    values = parse_qs(urlparse(link["url"]).query).get(param)
    return values[0] if values else None


def page_cursors(response: Response) -> SocialPageCursors:
    """Read the paging cursors from Social's ``Link`` header.

    Social sends ``rel="next"`` (``max_id``) only while an older page may exist
    and ``rel="prev"`` (``min_id``) whenever the page is not empty; a missing
    header is an empty page, not an error.
    """
    links = response.links
    return SocialPageCursors(
        next_max_id=_cursor(links.get("next"), "max_id"),
        prev_min_id=_cursor(links.get("prev"), "min_id"),
    )


def _clamp(limit: int, ceiling: int) -> int:
    return min(max(1, limit), ceiling)


def _cursor_params(
    max_id: str | None, min_id: str | None, since_id: str | None
) -> dict[str, str]:
    """The cursor query parameters that were actually given.

    Omitted rather than sent as ``0``: Social's defaults are ``0``, but a
    cursor that was never set should not appear on the wire at all.
    """
    params: dict[str, str] = {}
    for name, value in (("max_id", max_id), ("min_id", min_id), ("since_id", since_id)):
        if value is not None:
            params[name] = _validate_numeric_id(value, name)
    return params


class SocialClient(BaseNextcloudClient):
    """Client for the Nextcloud Social app."""

    app_name = "social"

    API_BASE = "/apps/social/api/v1"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Response:
        """Issue a Social API request with the headers every call needs.

        ``OCS_REQUEST_HEADERS`` is reused for its ``OCS-APIRequest`` half (see the
        module docstring); its ``Accept: application/json`` half is right here
        too, since every route used below answers JSON.
        """
        return await self._make_request(
            method,
            f"{self.API_BASE}{path}",
            headers=dict(OCS_REQUEST_HEADERS),
            **kwargs,
        )

    # Accounts

    async def verify_credentials(self) -> SocialAccount:
        """The caller's own account."""
        response = await self._request("GET", "/accounts/verify_credentials")
        return SocialAccount(**response.json())

    async def get_account(self, account: str) -> SocialAccount:
        """One account by numeric id or handle.

        A handle this instance has not seen is looked up on its own server.
        """
        ref = quote(validate_account(account), safe="@")
        response = await self._request("GET", f"/accounts/{ref}")
        return SocialAccount(**response.json())

    async def search_accounts(
        self,
        query: str,
        *,
        limit: int = 40,
        resolve: bool = False,
        following: bool = False,
    ) -> list[SocialAccount]:
        """Search accounts by name or handle.

        Args:
            query: Text to match against names and handles.
            limit: Maximum results, clamped to ``[1, 80]``.
            resolve: Also look a ``@user@host`` handle up on its own server.
            following: Only accounts the caller follows.
        """
        params: dict[str, Any] = {"q": query, "limit": _clamp(limit, MAX_SEARCH_LIMIT)}
        if resolve:
            params["resolve"] = "true"
        if following:
            params["following"] = "true"
        response = await self._request("GET", "/accounts/search", params=params)
        return [SocialAccount(**a) for a in response.json()]

    async def get_relationships(
        self, account_ids: list[str]
    ) -> list[SocialRelationship]:
        """The caller's relationship with each account.

        Sent as ``id[]=…``: Social declares the parameter as a PHP array and
        answers the singular ``id=…`` form with 400. Only numeric ids are
        accepted: Social resolves each entry as an id or an actor URL, never as a
        handle, so a handle would silently drop out of the answer.
        """
        ids = [_validate_numeric_id(a, "account id") for a in account_ids]
        response = await self._request(
            "GET", "/accounts/relationships", params=[("id[]", i) for i in ids]
        )
        return [SocialRelationship(**r) for r in response.json()]

    async def _account_list(
        self, account: str, which: str, limit: int, max_id: str | None
    ) -> tuple[list[SocialAccount], SocialPageCursors]:
        ref = quote(validate_account(account), safe="@")
        params: dict[str, Any] = {"limit": _clamp(limit, MAX_PAGE_LIMIT)}
        params.update(_cursor_params(max_id, None, None))
        response = await self._request("GET", f"/accounts/{ref}/{which}", params=params)
        return [SocialAccount(**a) for a in response.json()], page_cursors(response)

    async def get_followers(
        self, account: str, *, limit: int = 20, max_id: str | None = None
    ) -> tuple[list[SocialAccount], SocialPageCursors]:
        """Accounts following ``account``.

        For a remote account Social fetches the collection from its server,
        which does not page with ``max_id``; the cursors then come back empty.
        """
        return await self._account_list(account, "followers", limit, max_id)

    async def get_following(
        self, account: str, *, limit: int = 20, max_id: str | None = None
    ) -> tuple[list[SocialAccount], SocialPageCursors]:
        """Accounts ``account`` follows. Same paging caveat as followers."""
        return await self._account_list(account, "following", limit, max_id)

    async def follow(self, account: str) -> SocialRelationship:
        """Follow an account; a locked one leaves the relationship ``requested``."""
        ref = quote(validate_account(account), safe="@")
        response = await self._request("POST", f"/accounts/{ref}/follow")
        return SocialRelationship(**response.json())

    async def unfollow(self, account: str) -> SocialRelationship:
        """Unfollow an account (or withdraw a pending follow request)."""
        ref = quote(validate_account(account), safe="@")
        response = await self._request("POST", f"/accounts/{ref}/unfollow")
        return SocialRelationship(**response.json())

    # Timelines

    async def get_timeline(
        self,
        timeline: SocialTimeline,
        *,
        limit: int = 20,
        max_id: str | None = None,
        min_id: str | None = None,
        since_id: str | None = None,
    ) -> tuple[list[SocialStatus], SocialPageCursors]:
        """Read the home, local or federated timeline, newest first.

        ``local`` is not a route of its own: it is the ``public`` timeline with
        ``local=true``.
        """
        route = "home" if timeline == "home" else "public"
        params: dict[str, Any] = {"limit": _clamp(limit, MAX_PAGE_LIMIT)}
        if timeline == "local":
            params["local"] = "true"
        params.update(_cursor_params(max_id, min_id, since_id))
        # The trailing slash is part of the route as Social declares it
        # (``/api/v1/timelines/{timeline}/``); the other timeline routes have none.
        response = await self._request("GET", f"/timelines/{route}/", params=params)
        return [SocialStatus(**s) for s in response.json()], page_cursors(response)

    async def get_hashtag_timeline(
        self,
        hashtag: str,
        *,
        local: bool = False,
        limit: int = 20,
        max_id: str | None = None,
        min_id: str | None = None,
        since_id: str | None = None,
    ) -> tuple[list[SocialStatus], SocialPageCursors]:
        """Statuses carrying a hashtag, newest first."""
        tag = quote(validate_hashtag(hashtag), safe="")
        params: dict[str, Any] = {"limit": _clamp(limit, MAX_PAGE_LIMIT)}
        if local:
            params["local"] = "true"
        params.update(_cursor_params(max_id, min_id, since_id))
        response = await self._request("GET", f"/timelines/tag/{tag}", params=params)
        return [SocialStatus(**s) for s in response.json()], page_cursors(response)

    async def get_account_statuses(
        self,
        account: str,
        *,
        limit: int = 20,
        max_id: str | None = None,
        min_id: str | None = None,
        since_id: str | None = None,
    ) -> tuple[list[SocialStatus], SocialPageCursors]:
        """Statuses posted by one account, newest first."""
        ref = quote(validate_account(account), safe="@")
        params: dict[str, Any] = {"limit": _clamp(limit, MAX_PAGE_LIMIT)}
        params.update(_cursor_params(max_id, min_id, since_id))
        response = await self._request(
            "GET", f"/accounts/{ref}/statuses", params=params
        )
        return [SocialStatus(**s) for s in response.json()], page_cursors(response)

    # Statuses

    async def get_status(self, status_id: str) -> SocialStatus:
        """One status by id."""
        response = await self._request(
            "GET", f"/statuses/{validate_status_id(status_id)}"
        )
        return SocialStatus(**response.json())

    async def get_status_context(self, status_id: str) -> SocialContext:
        """The ancestors and descendants of a status."""
        response = await self._request(
            "GET", f"/statuses/{validate_status_id(status_id)}/context"
        )
        return SocialContext(**response.json())

    async def post_status(
        self,
        status: str,
        *,
        visibility: SocialVisibility | None = None,
        in_reply_to_id: str | None = None,
        spoiler_text: str | None = None,
        language: str | None = None,
    ) -> SocialStatus:
        """Publish a status.

        Every optional field is sent only when given. ``visibility`` in
        particular: left out, Social applies the account's own default
        (``source[privacy]``), and choosing one here would override the
        account owner's decision.

        An ``in_reply_to_id`` Social cannot find does not fail the call -- the
        post is published as a top-level status (``ApiController::statusNew``).
        """
        body: dict[str, Any] = {"status": status}
        if visibility is not None:
            body["visibility"] = visibility
        if in_reply_to_id is not None:
            body["in_reply_to_id"] = validate_status_id(in_reply_to_id)
        if spoiler_text is not None:
            body["spoiler_text"] = spoiler_text
        if language is not None:
            body["language"] = language
        response = await self._request("POST", "/statuses", json=body)
        return SocialStatus(**response.json())

    async def delete_status(self, status_id: str) -> SocialStatus:
        """Delete one of the caller's own statuses; returns what was removed.

        Somebody else's status answers 404, the same as an unknown id.
        """
        response = await self._request(
            "DELETE", f"/statuses/{validate_status_id(status_id)}"
        )
        return SocialStatus(**response.json())

    async def status_action(self, status_id: str, action: str) -> SocialStatus:
        """Favourite/unfavourite or boost/unboost a status; returns the status.

        Social names boosting ``reblog``/``unreblog`` and refuses ``boost``.
        """
        if action not in ("favourite", "unfavourite", "reblog", "unreblog"):
            raise ValueError(f"Unsupported status action {action!r}")
        response = await self._request(
            "POST", f"/statuses/{validate_status_id(status_id)}/{action}"
        )
        return SocialStatus(**response.json())

    # Notifications

    async def get_notifications(
        self,
        *,
        limit: int = 20,
        max_id: str | None = None,
        min_id: str | None = None,
        since_id: str | None = None,
        types: list[str] | None = None,
        exclude_types: list[str] | None = None,
    ) -> tuple[list[SocialNotification], SocialPageCursors]:
        """The caller's notifications, newest first.

        ``types`` / ``exclude_types`` are PHP array parameters, sent as
        ``types[]=…`` for the same reason as relationships' ``id[]``.
        """
        params: list[tuple[str, str]] = [("limit", str(_clamp(limit, MAX_PAGE_LIMIT)))]
        params.extend(_cursor_params(max_id, min_id, since_id).items())
        params.extend(("types[]", t) for t in types or ())
        params.extend(("exclude_types[]", t) for t in exclude_types or ())
        response = await self._request("GET", "/notifications", params=params)
        return [SocialNotification(**n) for n in response.json()], page_cursors(
            response
        )
