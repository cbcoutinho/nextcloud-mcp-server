"""Pydantic models for Nextcloud News app responses."""

from typing import List

from pydantic import BaseModel, ConfigDict, Field

from .base import BaseResponse


class NewsFolder(BaseModel):
    """Model for a News folder."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(description="Folder ID")
    name: str = Field(description="Folder name")


class NewsFeed(BaseModel):
    """Model for a News feed (RSS/Atom subscription)."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(description="Feed ID")
    url: str = Field(description="Feed URL")
    title: str = Field(description="Feed title")
    favicon_link: str | None = Field(
        None, alias="faviconLink", description="Favicon URL"
    )
    link: str | None = Field(None, description="Website link")
    added: int = Field(description="Unix timestamp when feed was added")
    folder_id: int | None = Field(
        None, alias="folderId", description="Parent folder ID"
    )
    unread_count: int = Field(
        0, alias="unreadCount", description="Number of unread items"
    )
    ordering: int = Field(
        0, description="Feed ordering (0=default, 1=oldest, 2=newest)"
    )
    pinned: bool = Field(False, description="Whether feed is pinned to top")
    update_error_count: int = Field(
        0, alias="updateErrorCount", description="Consecutive update failures"
    )
    last_update_error: str | None = Field(
        None, alias="lastUpdateError", description="Last update error message"
    )

    @property
    def has_errors(self) -> bool:
        """Check if feed has update errors."""
        return self.update_error_count > 0


class NewsItem(BaseModel):
    """Model for a News item (article) with full content."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(description="Item ID")
    guid: str = Field(description="Globally unique identifier")
    guid_hash: str = Field(alias="guidHash", description="MD5 hash of GUID")
    url: str | None = Field(None, description="Article URL")
    title: str = Field(description="Article title")
    author: str | None = Field(None, description="Article author")
    pub_date: int | None = Field(
        None, alias="pubDate", description="Publication timestamp"
    )
    body: str | None = Field(None, description="Article content (HTML)")
    enclosure_mime: str | None = Field(
        None, alias="enclosureMime", description="Enclosure MIME type"
    )
    enclosure_link: str | None = Field(
        None, alias="enclosureLink", description="Enclosure URL"
    )
    media_thumbnail: str | None = Field(
        None, alias="mediaThumbnail", description="Media thumbnail URL"
    )
    media_description: str | None = Field(
        None, alias="mediaDescription", description="Media description"
    )
    feed_id: int = Field(alias="feedId", description="Parent feed ID")
    unread: bool = Field(True, description="Whether item is unread")
    starred: bool = Field(False, description="Whether item is starred")
    rtl: bool = Field(False, description="Right-to-left text")
    last_modified: int = Field(
        alias="lastModified", description="Last modification timestamp"
    )
    fingerprint: str | None = Field(
        None, description="Content fingerprint for deduplication"
    )
    content_hash: str | None = Field(
        None, alias="contentHash", description="Content hash"
    )


class NewsItemSummary(BaseModel):
    """Lightweight model for News item list responses."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(description="Item ID")
    title: str = Field(description="Article title")
    feed_id: int = Field(alias="feedId", description="Parent feed ID")
    unread: bool = Field(True, description="Whether item is unread")
    starred: bool = Field(False, description="Whether item is starred")
    pub_date: int | None = Field(
        None, alias="pubDate", description="Publication timestamp"
    )
    url: str | None = Field(None, description="Article URL")
    author: str | None = Field(None, description="Article author")


class NewsStatus(BaseModel):
    """Model for News app status."""

    version: str = Field(description="News app version")
    warnings: dict = Field(default_factory=dict, description="Configuration warnings")


# --- Response Models ---


class ListFoldersResponse(BaseResponse):
    """Response model for listing folders."""

    results: List[NewsFolder] = Field(description="List of folders")
    total_count: int = Field(description="Total number of folders")


class ListFeedsResponse(BaseResponse):
    """Response model for listing feeds."""

    results: List[NewsFeed] = Field(description="List of feeds")
    starred_count: int = Field(0, description="Number of starred items")
    newest_item_id: int | None = Field(None, description="ID of newest item")
    total_count: int = Field(description="Total number of feeds")


class ListItemsResponse(BaseResponse):
    """Response model for listing items."""

    results: List[NewsItemSummary] = Field(description="List of items")
    total_count: int = Field(description="Number of items returned")
    has_more: bool = Field(False, description="Whether more items exist")
    oldest_id: int | None = Field(None, description="Oldest item ID (for pagination)")


class GetItemResponse(BaseResponse):
    """Response model for getting a single item."""

    item: NewsItem = Field(description="Full item details")


class FeedHealthResponse(BaseResponse):
    """Response model for feed health status."""

    feed_id: int = Field(description="Feed ID")
    title: str = Field(description="Feed title")
    url: str = Field(description="Feed URL")
    has_errors: bool = Field(description="Whether feed has update errors")
    error_count: int = Field(description="Number of consecutive errors")
    last_error: str | None = Field(None, description="Last error message")


class GetStatusResponse(BaseResponse):
    """Response model for app status."""

    version: str = Field(description="News app version")
    warnings: dict = Field(default_factory=dict, description="Configuration warnings")
