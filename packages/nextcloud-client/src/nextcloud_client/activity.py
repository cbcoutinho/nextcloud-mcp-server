"""Client for the Activity app's OCS API v2.

The stream is read through a cursor: ``since`` is an activity id, and the server
answers with the cursor to continue from in ``X-Activity-Last-Given``. Three
properties of that API decide how a page is read, and they hold on every
supported release (the activity app bundled with Nextcloud 33 to 35, and its
9.x development line):

* **A cursor can be silently reset.** Nextcloud 33-35 restart the stream at its
  first activity when ``since`` names an activity that does not exist (expired,
  or never valid), and answer 200. The only trace is ``X-Activity-First-Known``,
  which the server sets exactly when it ignored ``since``. So a request that sent
  a cursor and got that header back was not a continuation.
* **The end of the stream is the missing ``Link: rel="next"`` header.** Several
  rows are grouped into one activity, so a page shorter than ``limit`` says
  nothing about whether more follow.
* **304 is "nothing new", not an error.** The server answers 304 when the page
  is empty or the ``If-None-Match`` etag matches, and 204 when the user has no
  activity settings enabled.
"""

import re
from typing import Any

from httpx2 import HTTPStatusError, Response

from .base import BaseNextcloudClient
from .models.activity import ActivityPage
from .ocs import OCS_REQUEST_HEADERS

API_BASE = "/ocs/v2.php/apps/activity/api/v2/activity"

#: Filter ids are app-defined slugs (``files``, ``calendar_todo``, ...). The id is
#: a path segment, so anything else could walk to another OCS route.
FILTER_ID = re.compile(r"[A-Za-z0-9_-]+")


def _int_header(response: Response, name: str) -> int | None:
    """A positive integer header, or None. The 9.x line sends ``0`` for "none"."""
    try:
        value = int(response.headers.get(name, ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _normalise_objects(objects: Any) -> dict[str, str]:
    """``objects`` as ``{object_id: name}``.

    PHP encodes an empty map as ``[]``, so anything but a JSON object is empty.
    """
    if not isinstance(objects, dict):
        return {}
    return {str(k): str(v) for k, v in objects.items()}


class ActivityClient(BaseNextcloudClient):
    """Client for the Nextcloud Activity stream."""

    app_name = "activity"

    async def list_filters(self) -> list[dict[str, Any]]:
        """The stream filters this instance offers (``all``, ``files``, ...)."""
        response = await self._make_request(
            "GET", f"{API_BASE}/filters", headers=dict(OCS_REQUEST_HEADERS)
        )
        return response.json()["ocs"]["data"]

    async def list_activities(
        self,
        filter: str = "all",
        since: int = 0,
        limit: int = 50,
        sort: str = "desc",
        object_type: str = "",
        object_id: int = 0,
    ) -> ActivityPage:
        """Read one page of the activity stream.

        Args:
            filter: Stream filter id from :meth:`list_filters`. Ignored when
                ``object_id`` is given: the server's object filter is used then.
            since: Activity id to continue after (0 to start at the beginning)
            limit: Rows to read; the server caps it at 200
            sort: ``asc`` (oldest first) or ``desc`` (newest first)
            object_type: With ``object_id``, only activity about that object
            object_id: See ``object_type``

        Raises:
            ValueError: ``filter`` is not a valid filter id
            HTTPStatusError: 404 for an unknown filter, 403 for a cursor that
                belongs to another user (Nextcloud 33-35)
        """
        if not FILTER_ID.fullmatch(filter):
            raise ValueError(f"Invalid activity filter id: {filter!r}")
        params: dict[str, Any] = {"since": since, "limit": limit, "sort": sort}
        path = f"{API_BASE}/{filter}"
        if object_id:
            path = f"{API_BASE}/filter"
            params |= {"object_type": object_type, "object_id": object_id}
        try:
            response = await self._make_request(
                "GET", path, params=params, headers=dict(OCS_REQUEST_HEADERS)
            )
        except HTTPStatusError as e:
            if e.response.status_code != 304:
                raise
            response = e.response

        activities: list[dict[str, Any]] = []
        if response.status_code == 200:
            activities = response.json()["ocs"]["data"] or []
            for activity in activities:
                activity["objects"] = _normalise_objects(activity.get("objects"))

        first_known = _int_header(response, "X-Activity-First-Known")
        return ActivityPage(
            activities=activities,
            last_given=_int_header(response, "X-Activity-Last-Given"),
            first_known=first_known,
            has_more='rel="next"' in response.headers.get("Link", ""),
            etag=response.headers.get("ETag"),
            cursor_reset=since > 0 and first_known is not None,
        )
