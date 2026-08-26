"""Pydantic models for WebDAV responses."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from .base import BaseResponse, StatusResponse

#: Outcome of a document parse, and the shape of what came back. Defined here
#: (the response contract) so the tool, the parser and the model cannot drift
#: into three slightly different vocabularies.
ParseStatus = Literal["parsed", "failed", "skipped", "not_applicable"]
ContentFormat = Literal["text", "markdown", "base64"]


class FileInfo(BaseModel):
    """Model for file/directory information."""

    name: str = Field(description="File/directory name")
    path: str = Field(description="Full path")
    is_directory: bool = Field(description="Whether this is a directory")
    size: Optional[int] = Field(
        None, description="File size in bytes (None for directories)"
    )
    content_type: Optional[str] = Field(None, description="MIME content type")
    last_modified: Optional[str] = Field(
        None, description="Last modification time (ISO format)"
    )
    etag: Optional[str] = Field(None, description="ETag for versioning")
    file_id: Optional[int] = Field(None, description="Nextcloud file ID")
    is_favorite: Optional[bool] = Field(None, description="Whether file is favorited")
    url: str | None = Field(
        default=None,
        description=(
            "Link that opens this file or folder in Nextcloud. Offer it to the "
            "user when referring to the file so they can open it in place. None "
            "when the server has no browser-reachable Nextcloud base URL "
            "configured, or when this entry carries no file_id."
        ),
    )

    @property
    def last_modified_datetime(self) -> Optional[datetime]:
        """Convert last modified string to datetime."""
        if not self.last_modified:
            return None
        try:
            return datetime.fromisoformat(self.last_modified.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None


class DirectoryListing(BaseResponse):
    """Response model for directory listings."""

    path: str = Field(description="Directory path")
    files: List[FileInfo] = Field(description="Files and directories in the path")
    total_count: int = Field(description="Total number of items")
    directories_count: int = Field(description="Number of directories")
    files_count: int = Field(description="Number of files")
    total_size: int = Field(default=0, description="Total size of all files in bytes")


class ReadFileResponse(BaseResponse):
    """Response model for reading file contents."""

    path: str = Field(description="File path")
    content: str = Field(description="File content (text or base64 for binary)")
    content_type: str = Field(description="MIME content type")
    size: int = Field(description="File size in bytes (raw, pre-parse)")
    encoding: Optional[str] = Field(
        None, description="Encoding used (e.g., 'base64' for binary files)"
    )
    parsed: bool = Field(
        default=False,
        description=(
            "Whether text was successfully extracted from a document "
            "(PDF, DOCX, ...). Equivalent to parse_status == 'parsed'."
        ),
    )
    parse_status: ParseStatus = Field(
        default="not_applicable",
        description=(
            "Outcome of the document parse: 'parsed' (content is extracted text), "
            "'failed' (a parse was attempted and did not produce text -- see "
            "parse_notes), 'skipped' (the caller asked for the raw file), or "
            "'not_applicable' (nothing here needed parsing, e.g. a text file or a "
            "type no processor handles)."
        ),
    )
    parse_tier: Optional[str] = Field(
        None,
        description=(
            "Extraction tier that produced the content: 'fast' (plain text layer), "
            "'structured' (markdown reconstruction) or 'ocr'."
        ),
    )
    parse_processor: Optional[str] = Field(
        None, description="Name of the processor that produced the content"
    )
    content_format: ContentFormat = Field(
        default="text",
        description=(
            "What `content` actually is: markdown with headings/tables, plain "
            "text, or base64-encoded bytes."
        ),
    )
    parse_notes: List[str] = Field(
        default_factory=list,
        description=(
            "Plain statements about anything that degraded this read -- OCR "
            "unavailable, markdown structure not reconstructed, size cap hit, "
            "parse failed. When non-empty, report them; the content is not the "
            "whole document."
        ),
    )
    parsing_metadata: Optional[dict] = Field(
        None, description="Raw document-processor metadata when a parse ran"
    )
    etag: Optional[str] = Field(None, description="ETag for versioning")
    last_modified: Optional[str] = Field(None, description="Last modification time")
    url: str | None = Field(
        default=None,
        description=(
            "Link that opens this file in Nextcloud. Offer it alongside the "
            "content so the user can open the original -- particularly when "
            "`parse_notes` says the extraction was degraded and what you have "
            "is not the whole document. None when the server has no "
            "browser-reachable Nextcloud base URL configured, or when the file "
            "has no resolvable fileid."
        ),
    )


class WriteFileResponse(StatusResponse):
    """Response model for writing files."""

    path: str = Field(description="File path that was written")
    size: Optional[int] = Field(
        None,
        description="Size of the written file in bytes (decoded content, not the "
        "caller-supplied string length)",
    )
    created: bool = Field(description="Whether a new file was created (vs overwritten)")
    etag: Optional[str] = Field(
        None,
        description=(
            "ETag of the file as written. Pass it straight back as `if_match` on "
            "the next write to chain edits without re-reading. None if the server "
            "did not return one (some proxies strip it) — re-read the file to "
            "obtain it in that case."
        ),
    )


class CreateDirectoryResponse(StatusResponse):
    """Response model for directory creation."""

    path: str = Field(description="Directory path that was created")
    created: bool = Field(
        description="Whether directory was created or already existed"
    )


class DeleteResourceResponse(StatusResponse):
    """Response model for resource deletion."""

    path: str = Field(description="Path that was deleted")
    was_directory: bool = Field(
        description="Whether the deleted resource was a directory"
    )
    items_deleted: Optional[int] = Field(
        None, description="Number of items deleted (for directories)"
    )


class MoveResourceResponse(StatusResponse):
    """Response model for resource move/rename operations."""

    source_path: str = Field(description="Original path of the resource")
    destination_path: str = Field(description="New path of the resource")
    overwrite: bool = Field(
        description="Whether the destination was overwritten if it existed"
    )


class CopyResourceResponse(StatusResponse):
    """Response model for resource copy operations."""

    source_path: str = Field(description="Original path of the resource")
    destination_path: str = Field(description="Destination path for the copy")
    overwrite: bool = Field(
        description="Whether the destination was overwritten if it existed"
    )


class FileComment(BaseModel):
    """A single comment on a file."""

    id: int = Field(description="Comment ID")
    message: str = Field(description="Comment text, mentions included as typed")
    actor_id: str = Field(description="User ID of the comment's author")
    actor_type: str = Field(description="Actor type, normally 'users'")
    actor_display_name: Optional[str] = Field(
        None, description="Display name of the comment's author"
    )
    creation_datetime: Optional[str] = Field(
        None, description="When the comment was posted (RFC 1123 date)"
    )
    verb: str = Field(description="Comment verb, normally 'comment'")
    is_unread: bool = Field(
        default=False, description="Whether the comment is unread by you"
    )


class ListFileCommentsResponse(BaseResponse):
    """Response model for listing the comments on a file."""

    results: List[FileComment] = Field(description="Comments, newest first")
    count: int = Field(
        description=(
            "Number of comments in this page. Nextcloud does not report a "
            "thread total — a full page means there may be more, so page with "
            "`offset`."
        )
    )
    path: str = Field(description="Path of the commented file")
    file_id: int = Field(description="Nextcloud file ID the comments belong to")
    limit: int = Field(description="Page size that was requested")
    offset: int = Field(description="Number of newest comments that were skipped")


class CreateFileCommentResponse(BaseResponse):
    """Response model for posting a comment on a file."""

    path: str = Field(description="Path of the commented file")
    file_id: int = Field(description="Nextcloud file ID the comment was posted on")
    comment_id: Optional[int] = Field(
        None,
        description=(
            "ID of the created comment. None if the server did not name the new "
            "comment's location — the comment was still posted."
        ),
    )
    message: str = Field(description="The comment text that was posted")


class SearchFilesResponse(BaseResponse):
    """Response model for WebDAV search operations."""

    results: List[FileInfo] = Field(description="Search results")
    total_found: int = Field(description="Total number of files found")
    scope: str = Field(description="The scope/path that was searched")
    filters_applied: Optional[dict] = Field(
        None, description="Filters that were applied to the search"
    )


class SystemTag(BaseModel):
    """A system tag defined on the instance."""

    id: int = Field(description="Tag id")
    name: str = Field(description="Tag display name")
    assignable: bool = Field(
        default=True, description="Whether users may assign this tag"
    )


class ListTagsResponse(BaseResponse):
    """Every system tag on the instance."""

    tags: List[SystemTag] = Field(default_factory=list, description="Tags, by name")
    total_count: int = Field(description="Number of tags")


class FileTagsResponse(BaseResponse):
    """Tags assigned to one file."""

    path: str = Field(description="Path the tags belong to")
    file_id: str = Field(description="Nextcloud file id")
    tags: List[SystemTag] = Field(default_factory=list, description="Assigned tags")


class FilesByTagResponse(BaseResponse):
    """Files carrying a given tag."""

    tag: str = Field(description="Tag name that was searched for")
    tag_id: Optional[int] = Field(None, description="Tag id, when the tag exists")
    files: List[FileInfo] = Field(default_factory=list, description="Matching files")
    total_count: int = Field(description="Number of matching files")


class TagFileResponse(BaseResponse):
    """Outcome of attaching or detaching a tag."""

    path: str = Field(description="Path that was tagged or untagged")
    tag: str = Field(description="Tag name")
    tag_id: int = Field(description="Tag id")
    assigned: bool = Field(description="True after tagging, False after untagging")
