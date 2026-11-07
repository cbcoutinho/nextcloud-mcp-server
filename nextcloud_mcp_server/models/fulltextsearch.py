"""Pydantic models for the Nextcloud Full Text Search tools."""

from pydantic import BaseModel, Field

from .base import BaseResponse


class FullTextSearchHit(BaseModel):
    """One file found by the full text index."""

    path: str = Field(description="Path of the file, usable with nc_webdav_read_file")
    mime_type: str | None = Field(default=None, description="MIME type of the file")
    size: int | None = Field(default=None, description="Size of the file in bytes")
    modified: str | None = Field(
        default=None, description="Last modification as ISO 8601 timestamp (UTC)"
    )
    excerpts: list[str] = Field(
        default_factory=list, description="Short text excerpts around the matches"
    )


class FullTextSearchResponse(BaseResponse):
    """Response of a full text search."""

    query: str = Field(description="The query that was sent")
    total: int = Field(
        description="Number of all hits, also beyond this page. The search platform "
        "may stop counting, Elasticsearch at 10000."
    )
    page: int = Field(description="Page of this response, starting at 1")
    limit: int = Field(description="Results per page")
    has_more: bool = Field(description="Whether further pages exist")
    results: list[FullTextSearchHit] = Field(
        default_factory=list, description="The hits of this page"
    )
    note: str | None = Field(
        default=None, description="Explains an unexpected result, for the user"
    )
