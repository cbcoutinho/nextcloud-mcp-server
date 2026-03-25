"""Pydantic models for Nextcloud Collectives app."""

import re

from pydantic import BaseModel, Field, field_validator

from .base import BaseResponse, StatusResponse

# Domain Models


class Collective(BaseModel):
    """A Nextcloud Collective (wiki/knowledge base)."""

    id: int = Field(description="Collective ID")
    circleId: str = Field(description="Linked Circle/Team ID")
    emoji: str | None = Field(default=None, description="Collective emoji")
    name: str = Field(description="Collective name")
    level: int = Field(description="User's membership level")
    canEdit: bool = Field(description="Whether the user can edit")
    canShare: bool = Field(description="Whether the user can share")
    pageMode: int = Field(description="Default page mode: 0=view, 1=edit")


class PageInfo(BaseModel):
    """A page within a Collective."""

    id: int = Field(description="Page ID")
    title: str = Field(description="Page title")
    emoji: str | None = Field(default=None, description="Page emoji")
    fileName: str = Field(description="Markdown file name")
    filePath: str = Field(description="File path within the collective")
    collectivePath: str | None = Field(
        default=None, description="Collective folder path in user's files"
    )
    parentId: int = Field(description="Parent page ID (0 for root)")
    timestamp: int = Field(description="Last modification Unix timestamp")
    size: int = Field(description="Content size in bytes")
    lastUserId: str | None = Field(default=None, description="Last editor user ID")
    lastUserDisplayName: str | None = Field(
        default=None, description="Last editor display name"
    )
    subpageOrder: list[int] = Field(
        default_factory=list, description="Ordered subpage IDs"
    )
    isFullWidth: bool | None = Field(default=None, description="Full-width page layout")
    trashTimestamp: int | None = Field(
        default=None, description="Timestamp when the page was trashed"
    )


class CollectiveTag(BaseModel):
    """A tag within a Collective."""

    id: int = Field(description="Tag ID")
    collectiveId: int = Field(description="Parent collective ID")
    name: str = Field(description="Tag name")
    color: str = Field(description="Hex color code (e.g. 'FF0000')")

    @field_validator("color")
    @classmethod
    def validate_hex_color(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9A-Fa-f]{3,8}", v):
            raise ValueError(f"Invalid hex color: {v!r}")
        return v


# Response Models


class ListCollectivesResponse(BaseResponse):
    """Response for listing collectives."""

    collectives: list[Collective] = Field(description="List of collectives")
    total: int = Field(description="Total number of collectives")


class CreateCollectiveResponse(BaseResponse):
    """Response for creating a collective."""

    id: int = Field(description="Created collective ID")
    name: str = Field(description="Created collective name")
    emoji: str | None = Field(default=None, description="Collective emoji")


class CollectiveOperationResponse(StatusResponse):
    """Response for collective update operations."""

    collective_id: int = Field(description="ID of the affected collective")


class ListPagesResponse(BaseResponse):
    """Response for listing pages in a collective."""

    pages: list[PageInfo] = Field(description="List of pages")
    total: int = Field(description="Total number of pages")
    collective_id: int = Field(description="Collective ID")


class GetPageResponse(BaseResponse):
    """Response for getting a single page with content."""

    page: PageInfo = Field(description="Page metadata")
    content: str | None = Field(
        default=None,
        description="Page markdown content (fetched via WebDAV)",
    )


class CreatePageResponse(BaseResponse):
    """Response for creating a page."""

    id: int = Field(description="Created page ID")
    title: str = Field(description="Created page title")
    collective_id: int = Field(description="Collective ID")
    parent_id: int = Field(description="Parent page ID")


class PageOperationResponse(StatusResponse):
    """Response for page operations (update, trash, emoji, tag)."""

    page_id: int = Field(description="ID of the affected page")
    collective_id: int = Field(description="Collective ID")


class SearchPagesResponse(BaseResponse):
    """Response for full-text search within a collective."""

    results: list[PageInfo] = Field(description="Matching pages")
    total: int = Field(description="Total number of results")
    query: str = Field(description="Search query")
    collective_id: int = Field(description="Collective ID")


class ListTrashedPagesResponse(ListPagesResponse):
    """Response for listing trashed pages in a collective."""

    is_trash: bool = Field(
        default=True, description="Indicates these are trashed pages"
    )


class ListTagsResponse(BaseResponse):
    """Response for listing tags in a collective."""

    tags: list[CollectiveTag] = Field(description="List of tags")
    total: int = Field(description="Total number of tags")
    collective_id: int = Field(description="Collective ID")


class CreateTagResponse(BaseResponse):
    """Response for creating a tag."""

    id: int = Field(description="Created tag ID")
    name: str = Field(description="Tag name")
    color: str = Field(description="Tag color")
