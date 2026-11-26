"""Data returned by the Activity client."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActivityPage:
    """One page of the activity stream, with the cursor state of the response."""

    #: Activities as the server sent them, ``objects`` normalised to a dict.
    activities: list[dict[str, Any]]
    #: ``X-Activity-Last-Given``: the cursor to continue from. None when the
    #: page was empty.
    last_given: int | None
    #: ``X-Activity-First-Known``: sent only when the server ignored ``since``.
    first_known: int | None
    #: The server sent a ``Link: rel="next"`` header.
    has_more: bool
    etag: str | None
    #: A cursor was sent and the server restarted the stream instead.
    cursor_reset: bool
