"""Pydantic models for Notes app responses."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from .base import BaseResponse, IdResponse, StatusResponse

#: Shared wording for the deep link, so all five notes responses describe it the
#: same way. Populated by ``links.with_links``; see ADR-035.
NOTE_URL_DESCRIPTION = (
    "Link that opens this note in the Nextcloud Notes app. Offer it to the user "
    "when referring to the note so they can read or edit it in place. None when "
    "the server has no browser-reachable Nextcloud base URL configured."
)


class Note(BaseModel):
    """Model for a Nextcloud note."""

    id: int = Field(description="Note ID")
    title: str = Field(description="Note title")
    content: str = Field(description="Note content in markdown")
    category: str = Field(default="", description="Note category")
    modified: int = Field(description="Unix timestamp of last modification")
    favorite: bool = Field(
        default=False, description="Whether note is marked as favorite"
    )
    etag: str = Field(description="ETag for versioning")
    readonly: bool = Field(default=False, description="Whether note is read-only")
    url: str | None = Field(default=None, description=NOTE_URL_DESCRIPTION)

    @property
    def modified_datetime(self) -> datetime:
        """Convert Unix timestamp to datetime."""
        return datetime.fromtimestamp(self.modified)


class NoteSearchResult(BaseModel):
    """Model for note search results (limited fields)."""

    id: int = Field(description="Note ID")
    title: str = Field(description="Note title")
    category: str = Field(default="", description="Note category")
    score: Optional[float] = Field(None, description="Search relevance score")
    url: str | None = Field(default=None, description=NOTE_URL_DESCRIPTION)


class NotesSettings(BaseModel):
    """Model for Notes app settings."""

    notesPath: str = Field(description="Path to notes directory")
    fileSuffix: str = Field(description="File suffix for notes")
    noteMode: str = Field(description="Note mode setting")


class CreateNoteResponse(IdResponse):
    """Response model for note creation."""

    title: str = Field(description="The created note title")
    category: str = Field(description="The created note category")
    etag: str = Field(description="Current ETag for the created note")
    url: str | None = Field(default=None, description=NOTE_URL_DESCRIPTION)


class UpdateNoteResponse(BaseResponse):
    """Response model for note updates."""

    id: int = Field(description="The updated note ID")
    title: str = Field(description="The updated note title")
    category: str = Field(description="The updated note category")
    etag: str = Field(description="Current ETag for the updated note")
    url: str | None = Field(default=None, description=NOTE_URL_DESCRIPTION)


class DeleteNoteResponse(StatusResponse):
    """Response model for note deletion."""

    deleted_id: int = Field(description="ID of the deleted note")


class AppendContentResponse(BaseResponse):
    """Response model for appending content to a note."""

    id: int = Field(description="The updated note ID")
    title: str = Field(description="The updated note title")
    category: str = Field(description="The updated note category")
    etag: str = Field(description="Current ETag for the updated note")
    url: str | None = Field(default=None, description=NOTE_URL_DESCRIPTION)


class SearchNotesResponse(BaseResponse):
    """Response model for note search."""

    results: List[NoteSearchResult] = Field(description="Search results")
    query: str = Field(description="The search query used")
    total_found: int = Field(description="Total number of notes found")
