"""Pydantic models for Notes app responses."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from .base import BaseResponse, IdResponse, StatusResponse


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


class SemanticSearchResult(BaseModel):
    """Model for semantic search results with additional metadata."""

    id: int = Field(description="Note ID")
    title: str = Field(description="Note title")
    category: str = Field(default="", description="Note category")
    excerpt: str = Field(description="Excerpt from matching chunk")
    score: float = Field(description="Semantic similarity score (0-1)")
    chunk_index: int = Field(description="Index of matching chunk in document")
    total_chunks: int = Field(description="Total number of chunks in document")


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


class UpdateNoteResponse(BaseResponse):
    """Response model for note updates."""

    id: int = Field(description="The updated note ID")
    title: str = Field(description="The updated note title")
    category: str = Field(description="The updated note category")
    etag: str = Field(description="Current ETag for the updated note")


class DeleteNoteResponse(StatusResponse):
    """Response model for note deletion."""

    deleted_id: int = Field(description="ID of the deleted note")


class AppendContentResponse(BaseResponse):
    """Response model for appending content to a note."""

    id: int = Field(description="The updated note ID")
    title: str = Field(description="The updated note title")
    category: str = Field(description="The updated note category")
    etag: str = Field(description="Current ETag for the updated note")


class SearchNotesResponse(BaseResponse):
    """Response model for note search."""

    results: List[NoteSearchResult] = Field(description="Search results")
    query: str = Field(description="The search query used")
    total_found: int = Field(description="Total number of notes found")


class SemanticSearchNotesResponse(BaseResponse):
    """Response model for semantic search."""

    results: List[SemanticSearchResult] = Field(
        description="Semantic search results with similarity scores"
    )
    query: str = Field(description="The search query used")
    total_found: int = Field(description="Total number of notes found")
    search_method: str = Field(
        default="semantic", description="Search method used (semantic or hybrid)"
    )
