"""Pydantic models for the Nextcloud Social app (Mastodon client API).

Social serialises Mastodon's entities (Account, Status, Notification,
Relationship). They are large, and every field costs an agent tokens on every
timeline read, so these models keep the fields an agent acts on and drop the
rest -- the same trade the Talk models make. Pydantic's default ``extra="ignore"``
is what does the dropping; it is deliberate, not an oversight.

Status ``content`` and account ``note`` arrive as HTML. They are exposed as plain
text (``text`` / ``note``): the markup is mention and hashtag ``<span>`` noise to
a model, and the structured ``mentions`` / ``tags`` fields carry the same
information without it.
"""

from html.parser import HTMLParser
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .base import BaseResponse

#: The timelines ``nc_social_get_timeline`` reads. ``local`` is Social's
#: ``public`` timeline narrowed with ``local=true``; the other two map 1:1.
SocialTimeline = Literal["home", "local", "public"]

#: Visibilities Social accepts on ``POST /api/v1/statuses``. ``private`` is
#: followers-only (Social also accepts ``followers`` as a synonym, which is left
#: out so the schema offers one spelling per meaning).
SocialVisibility = Literal["public", "unlisted", "private", "direct"]

#: Notification kinds Social serves on ``/api/v1/notifications`` (v0.24.1
#: ``docs/API.md``). A ``Literal`` so an unknown kind fails in the schema rather
#: than silently selecting nothing server-side.
SocialNotificationType = Literal[
    "mention",
    "reblog",
    "favourite",
    "update",
    "follow",
    "follow_request",
    "poll",
    "status",
    "moderation_warning",
    "severed_relationships",
]


class _TextExtractor(HTMLParser):
    """Collect the text of a Mastodon HTML fragment, keeping line structure.

    Mastodon content is ``<p>`` paragraphs with ``<br>`` line breaks. Links are
    shortened for display by wrapping parts of the URL in ``<span
    class="invisible">``; those spans are kept, because their text is part of
    the real URL.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(fragment: str | None) -> str:
    """Render a Mastodon HTML fragment as plain text. ``None`` becomes ``""``."""
    if not fragment:
        return ""
    parser = _TextExtractor()
    parser.feed(fragment)
    parser.close()
    return "".join(parser.parts).strip()


class _SocialEntity(BaseModel):
    """Base for the Mastodon entities below.

    Mastodon ids are strings on the wire and Social follows that, but a few
    entities have carried integers in past releases (``Relationship.id`` did
    until it was fixed for Swift clients). Coercing numbers to strings keeps an
    id's type stable for callers whichever the server sends.
    """

    model_config = ConfigDict(coerce_numbers_to_str=True)


class SocialAccountSummary(_SocialEntity):
    """The author of a status or notification: who, not their whole profile."""

    id: str = Field(description="Account ID")
    acct: str = Field(
        description="Handle: 'user' for a local account, 'user@host' for a remote one"
    )
    display_name: str = Field(default="", description="Display name")
    bot: bool = Field(default=False, description="Whether the account is automated")


class SocialAccount(SocialAccountSummary):
    """A Social account (Mastodon ``Account`` entity), trimmed."""

    username: str = Field(description="Username, without the host")
    note: str = Field(default="", description="Profile bio, as plain text")
    url: str | None = Field(None, description="Profile URL (the ActivityPub actor id)")
    locked: bool = Field(
        default=False, description="Whether follow requests need manual approval"
    )
    followers_count: int = Field(default=0, description="Number of followers")
    following_count: int = Field(default=0, description="Number of accounts followed")
    statuses_count: int = Field(default=0, description="Number of statuses posted")
    created_at: str | None = Field(None, description="Creation time (ISO 8601)")
    last_status_at: str | None = Field(
        None, description="When the account last posted, or null if it never has"
    )

    @model_validator(mode="before")
    @classmethod
    def _note_as_text(cls, data: Any) -> Any:
        if isinstance(data, dict) and "note" in data:
            data = {**data, "note": html_to_text(data["note"])}
        return data


class SocialMention(_SocialEntity):
    """An account mentioned in a status."""

    id: str = Field(description="Account ID")
    acct: str = Field(description="Handle of the mentioned account")


class SocialMediaAttachment(_SocialEntity):
    """A file attached to a status."""

    id: str = Field(description="Attachment ID")
    type: str = Field(description="image, video, gifv, audio or unknown")
    url: str | None = Field(None, description="URL of the file")
    description: str | None = Field(None, description="Alt text")


class SocialStatus(_SocialEntity):
    """A status (Mastodon ``Status`` entity), trimmed.

    A boost is a status of its own whose ``reblog`` holds the boosted status;
    read the post from ``reblog`` in that case, not from ``text``.
    """

    id: str = Field(description="Status ID; use it for replies and actions")
    created_at: str | None = Field(None, description="Publication time (ISO 8601)")
    edited_at: str | None = Field(
        None, description="Last edit time, or null if never edited"
    )
    account: SocialAccountSummary | None = Field(None, description="The author")
    text: str = Field(default="", description="The post, as plain text")
    spoiler_text: str = Field(
        default="", description="Content warning shown before the text, if any"
    )
    visibility: str | None = Field(
        None, description="public, unlisted, private (followers-only) or direct"
    )
    language: str | None = Field(None, description="ISO 639 language code, if set")
    in_reply_to_id: str | None = Field(
        None, description="ID of the status this replies to, if it is a reply"
    )
    in_reply_to_account_id: str | None = Field(
        None, description="ID of the account being replied to"
    )
    url: str | None = Field(None, description="Canonical URL of the status")
    replies_count: int = Field(default=0, description="Number of replies")
    reblogs_count: int = Field(default=0, description="Number of boosts")
    favourites_count: int = Field(default=0, description="Number of favourites")
    favourited: bool = Field(default=False, description="Whether you favourited it")
    reblogged: bool = Field(default=False, description="Whether you boosted it")
    mentions: list[SocialMention] = Field(
        default_factory=list, description="Accounts mentioned"
    )
    tags: list[str] = Field(default_factory=list, description="Hashtags, without '#'")
    media_attachments: list[SocialMediaAttachment] = Field(
        default_factory=list, description="Attached media"
    )
    reblog: "SocialStatus | None" = Field(
        None, description="For a boost: the status that was boosted"
    )

    @model_validator(mode="before")
    @classmethod
    def _flatten_wire_shape(cls, data: Any) -> Any:
        """Turn HTML ``content`` into ``text`` and ``tags`` into bare names."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "content" in data:
            data["text"] = html_to_text(data.pop("content"))
        tags = data.get("tags")
        if isinstance(tags, list):
            data["tags"] = [
                t["name"] if isinstance(t, dict) else t
                for t in tags
                if isinstance(t, str) or (isinstance(t, dict) and "name" in t)
            ]
        return data


class SocialContext(_SocialEntity):
    """The thread around a status."""

    ancestors: list[SocialStatus] = Field(
        default_factory=list, description="Statuses above it, oldest first"
    )
    descendants: list[SocialStatus] = Field(
        default_factory=list, description="Replies below it"
    )


class SocialNotification(_SocialEntity):
    """A notification (Mastodon ``Notification`` entity)."""

    id: str = Field(description="Notification ID; the cursor for paging")
    type: str = Field(
        description="mention, reblog, favourite, follow, follow_request, update, ..."
    )
    created_at: str | None = Field(None, description="When it happened (ISO 8601)")
    account: SocialAccountSummary | None = Field(
        None, description="The account that caused it"
    )
    status: SocialStatus | None = Field(
        None, description="The status involved, for mention/reblog/favourite/update"
    )


class SocialRelationship(_SocialEntity):
    """Your relationship with one account."""

    id: str = Field(description="Account ID the relationship is with")
    following: bool = Field(default=False, description="You follow them")
    followed_by: bool = Field(default=False, description="They follow you")
    requested: bool = Field(
        default=False, description="Your follow request is awaiting their approval"
    )
    requested_by: bool = Field(
        default=False, description="They have asked to follow you"
    )
    blocking: bool = Field(default=False, description="You block them")
    muting: bool = Field(default=False, description="You mute them")
    showing_reblogs: bool = Field(
        default=True, description="Their boosts appear in your home timeline"
    )
    notifying: bool = Field(
        default=False, description="You are notified whenever they post"
    )


class SocialPageCursors(_SocialEntity):
    """Paging cursors, taken from the ``Link`` header Social sends.

    Social pages on the unfiltered query, so these can differ from the ids of
    the items returned when a keyword filter hid some of them -- which is why
    they are read from the header rather than computed from the items.
    """

    next_max_id: str | None = Field(
        None,
        description=(
            "Pass as max_id to fetch the next, older page. Null when there is "
            "no older page"
        ),
    )
    prev_min_id: str | None = Field(
        None,
        description=(
            "Pass as min_id on a later call to fetch only newer items. Null on "
            "an empty page: keep the cursor you already had"
        ),
    )


# Response models for MCP tools


class SocialAccountResponse(BaseResponse):
    """One account."""

    account: SocialAccount = Field(description="The account")


class SocialAccountListResponse(BaseResponse):
    """A list or page of accounts."""

    accounts: list[SocialAccount] = Field(description="The accounts")
    count: int = Field(description="Number of accounts returned in this page")
    cursors: SocialPageCursors | None = Field(
        None, description="Paging cursors; null where the route does not page"
    )


class SocialStatusResponse(BaseResponse):
    """One status."""

    status: SocialStatus = Field(description="The status")


class SocialStatusListResponse(BaseResponse):
    """A page of statuses, newest first."""

    statuses: list[SocialStatus] = Field(description="The statuses, newest first")
    count: int = Field(description="Number of statuses returned in this page")
    cursors: SocialPageCursors = Field(description="Paging cursors")


class SocialContextResponse(BaseResponse):
    """A status together with its thread."""

    status_id: str = Field(description="ID of the status the thread was read for")
    context: SocialContext = Field(description="Ancestors and descendants")


class SocialNotificationListResponse(BaseResponse):
    """A page of notifications, newest first."""

    notifications: list[SocialNotification] = Field(
        description="The notifications, newest first"
    )
    count: int = Field(description="Number of notifications returned in this page")
    cursors: SocialPageCursors = Field(description="Paging cursors")


class SocialRelationshipListResponse(BaseResponse):
    """Relationships with the accounts asked about."""

    relationships: list[SocialRelationship] = Field(
        description="One entry per account Social could resolve"
    )


class SocialRelationshipResponse(BaseResponse):
    """The relationship after a follow or unfollow."""

    relationship: SocialRelationship = Field(description="The updated relationship")
