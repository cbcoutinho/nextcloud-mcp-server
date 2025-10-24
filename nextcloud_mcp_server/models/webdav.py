"""Pydantic models for WebDAV responses."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from .base import BaseResponse, StatusResponse


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
    size: int = Field(description="File size in bytes")
    encoding: Optional[str] = Field(
        None, description="Encoding used (e.g., 'base64' for binary files)"
    )
    etag: Optional[str] = Field(None, description="ETag for versioning")
    last_modified: Optional[str] = Field(None, description="Last modification time")


class WriteFileResponse(StatusResponse):
    """Response model for writing files."""

    path: str = Field(description="File path that was written")
    size: Optional[int] = Field(None, description="Size of the written file")
    created: bool = Field(description="Whether a new file was created (vs overwritten)")


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


class SearchFilesResponse(BaseResponse):
    """Response model for WebDAV search operations."""

    results: List[FileInfo] = Field(description="Search results")
    total_found: int = Field(description="Total number of files found")
    scope: str = Field(description="The scope/path that was searched")
    filters_applied: Optional[dict] = Field(
        None, description="Filters that were applied to the search"
    )
