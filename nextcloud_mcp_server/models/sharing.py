"""Pydantic models for Nextcloud sharing responses."""

from enum import IntEnum

from pydantic import Field

from .base import BaseResponse


class ShareType(IntEnum):
    """OCS ``shareType`` values.

    An ``IntEnum`` so it stays interchangeable with the bare ints the OCS API
    and our tool signatures use.

    Every type addresses a named recipient except :attr:`PUBLIC_LINK`, which
    addresses nobody — see ``validate_share_with`` in the sharing client for
    why that distinction has to be enforced before the request is sent.
    """

    USER = 0
    GROUP = 1
    PUBLIC_LINK = 3
    EMAIL = 4
    FEDERATED = 6
    CIRCLE = 7
    TALK_CONVERSATION = 10
    DECK_CARD = 12


class PublicDownloadLinkResponse(BaseResponse):
    """Response for a short-lived public download link (OCS ``shareType=3``).

    Lets MCP clients fetch the original binary file out-of-band (via
    ``download_url``) instead of receiving a base64 payload inline, which can
    exceed the client response budget and get truncated.
    """

    path: str = Field(description="Path of the shared file/folder")
    share_id: int = Field(description="OCS share ID (use to delete the link early)")
    url: str = Field(description="Public share page URL (e.g. https://host/s/<token>)")
    download_url: str = Field(
        description="Direct download URL for the original file (url + '/download')"
    )
    token: str | None = Field(
        None, description="Public share token embedded in the URL"
    )
    permissions: int = Field(
        description="Granted permissions (1 = read-only for a download link)"
    )
    expires_at: str | None = Field(
        None,
        description=(
            "Advisory RFC3339 instant the link was requested to expire. NOTE: "
            "Nextcloud enforces public-link expiry at date granularity — a link "
            "expires at 00:00:00 on expireDate in the owner's timezone (the end "
            "of the day before expireDate) — so the link may remain valid until "
            "the end of that day server-side."
        ),
    )
