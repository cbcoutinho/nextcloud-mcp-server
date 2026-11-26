"""MCP tools for the Nextcloud Activity stream (GH #1932).

They answer "what changed since I last looked": read the stream oldest first
from a saved cursor, and keep the returned cursor for next time. The cursor
mechanics are in ``nextcloud_client.activity``.

The stream holds only what Nextcloud already let the user see. On top of that,
``EXCLUDED_TAGS`` applies: one activity can group changes to several files, so
it is left out whole when any file it names is hidden. Redacting part of it
would leave the hidden path in its rich subject.
"""

from typing import Annotated, Any, Literal

from httpx2 import HTTPStatusError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.activity import (
    Activity,
    ActivityFilter,
    ListActivitiesResponse,
    ListActivityFiltersResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.server.tag_exclusion import (
    get_excluded_file_paths,
    is_path_excluded,
)


def _rich_file_paths(value: Any) -> list[str]:
    """Paths of every ``file`` parameter in a rich subject, at any depth."""
    if isinstance(value, list):
        return [p for item in value for p in _rich_file_paths(item)]
    if not isinstance(value, dict):
        return []
    paths = [p for item in value.values() for p in _rich_file_paths(item)]
    if value.get("type") == "file" and isinstance(value.get("path"), str):
        paths.append(value["path"])
    return paths


def activity_file_paths(activity: dict[str, Any]) -> list[str]:
    """Every file path an activity names: its objects and its rich subject/message.

    Objects are checked whatever the ``object_type``: a comment or share can name
    files too, and a non-path name that happens to match only hides one activity.
    """
    paths = _rich_file_paths(
        [activity.get("subject_rich"), activity.get("message_rich")]
    )
    paths += list(activity.get("objects", {}).values())
    if isinstance(name := activity.get("object_name"), str) and name:
        paths.append(name)
    return paths


def configure_activity_tools(mcp: MCPServer) -> None:
    """Configure Activity app MCP tools."""

    @mcp.tool(
        title="List Activity Filters",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("activity.read")
    @instrument_tool
    async def nc_activity_list_filters(ctx: Context) -> ListActivityFiltersResponse:
        """List the activity stream filters this Nextcloud offers.

        Common ones are ``all``, ``self`` (by you), ``by`` (by others) and
        ``files``. Apps add their own.
        """
        client = await get_client(ctx)
        filters = await client.activity.list_filters()
        return ListActivityFiltersResponse(
            filters=[ActivityFilter(**f) for f in filters]
        )

    @mcp.tool(
        title="List Activity",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("activity.read")
    @instrument_tool
    async def nc_activity_list(
        ctx: Context,
        filter: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")] = "all",
        since: Annotated[int | None, Field(ge=0)] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        sort: Literal["asc", "desc"] = "desc",
        object_id: Annotated[int | None, Field(ge=1)] = None,
        object_type: str = "files",
    ) -> ListActivitiesResponse:
        """Read the Nextcloud activity stream: who changed, shared or commented on what.

        To find what changed since a previous session, pass ``sort="asc"`` and
        the ``next_since`` you kept, read on while ``has_more`` is true, and keep
        the last ``next_since`` for next time. To start tracking, read the newest
        activity with ``sort="desc", limit=1`` and keep its ``activity_id``.

        If ``cursor_reset`` is true the cursor was not recognised and the stream
        restarted from the beginning, and changes may have been missed.

        One activity can group changes to several files: ``objects`` lists all of
        them, ``object_name`` only the first. The stream is not a complete audit
        log, and a file restored from the trash may get a new id.

        Args:
            filter: Stream filter, from nc_activity_list_filters
            since: Activity id to continue after. Omit to start at the newest
                (``desc``) or oldest (``asc``) activity
            limit: Activities to read, at most 200
            sort: ``asc`` for oldest first, ``desc`` for newest first
            object_id: Only activity about this object, e.g. a file id.
                Overrides ``filter``
            object_type: Type of ``object_id``
        """
        client = await get_client(ctx)
        try:
            page = await client.activity.list_activities(
                filter=filter,
                since=since or 0,
                limit=limit,
                sort=sort,
                object_type=object_type if object_id else "",
                object_id=object_id or 0,
            )
        except HTTPStatusError as e:
            status = e.response.status_code
            if status == 404:
                raise ToolError(
                    f"Unknown activity filter {filter!r}; see nc_activity_list_filters"
                ) from e
            if status == 403 and since:
                raise ToolError(
                    f"Activity cursor {since} is not valid for this user"
                ) from e
            raise ToolError(f"Failed to read activity: HTTP {status}") from e

        excluded = await get_excluded_file_paths(client.webdav)
        visible = [
            a
            for a in page.activities
            if not any(is_path_excluded(p, excluded) for p in activity_file_paths(a))
        ]
        return ListActivitiesResponse(
            activities=[Activity(**a) for a in visible],
            next_since=page.last_given or since,
            has_more=page.has_more,
            cursor_reset=page.cursor_reset,
            etag=page.etag,
            excluded_count=len(page.activities) - len(visible),
        )
