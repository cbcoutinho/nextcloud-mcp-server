"""Pydantic models for Nextcloud Activity responses."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nextcloud_mcp_server.models.base import BaseResponse


class ActivityFilter(BaseModel):
    """A stream filter the instance offers."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="Pass as `filter` to nc_activity_list")
    name: str = Field(description="Display name, in the user's language")


class ListActivityFiltersResponse(BaseResponse):
    filters: list[ActivityFilter]


class Activity(BaseModel):
    """One activity. It may group several changes into one entry."""

    model_config = ConfigDict(extra="ignore")

    activity_id: int
    datetime: str = Field(description="When it happened, ISO 8601")
    app: str
    type: str
    user: str | None = Field(default=None, description="Who did it")
    subject: str = Field(description="Human-readable summary")
    subject_rich: list[Any] = Field(
        default_factory=list,
        description="[template, parameters]: the subject with each referenced "
        "object (file, user, ...) as a structured parameter",
    )
    message: str = ""
    object_type: str = ""
    object_id: int = 0
    object_name: str = Field(
        default="",
        description="The primary object only. For a grouped entry, see `objects`.",
    )
    objects: dict[str, str] = Field(
        default_factory=dict,
        description="Every affected object, {object_id: name}. For files the name "
        "is the path at the time of the activity.",
    )
    link: str = ""


class ListActivitiesResponse(BaseResponse):
    """A page of the activity stream and the cursor to continue from."""

    activities: list[Activity]
    next_since: int | None = Field(
        description="Pass as `since` (with the same `sort`) to read on, or keep "
        "it for the next session. Unchanged from the request when the page was "
        "empty."
    )
    has_more: bool = Field(
        description="More activities follow right now. A short page does not mean "
        "the end: several changes can be grouped into one activity."
    )
    cursor_reset: bool = Field(
        description="The server did not recognise `since` (for example, it "
        "expired) and restarted at the beginning of the stream. Changes between "
        "the old cursor and now may be missing; fall back to a full scan."
    )
    etag: str | None = None
    excluded_count: int = Field(
        default=0,
        description="Activities left out because they touch a file hidden by "
        "EXCLUDED_TAGS",
    )
