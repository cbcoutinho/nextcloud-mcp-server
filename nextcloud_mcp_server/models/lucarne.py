"""Pydantic models for Lucarne app responses."""

from pydantic import BaseModel, ConfigDict, Field

from .base import BaseResponse


class LucarneChannel(BaseModel):
    """A channel subscription."""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="Channel ID")
    title: str = Field(description="Channel title")
    source_url: str = Field(description="YouTube channel URL")
    sync_status: str | None = Field(None, description="Synchronisation status")


class LucarneCatalog(BaseModel):
    """A catalogue grouping channel subscriptions."""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="Catalogue ID")
    name: str = Field(description="Catalogue name")
    channel_count: int | None = Field(
        None, description="Number of channels (list results only)"
    )
    channel_ids: list[int] | None = Field(
        None, description="IDs of the channels it contains (single results only)"
    )


class LucarnePlaylist(BaseModel):
    """A playlist, personal or imported from YouTube."""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="Playlist ID")
    title: str = Field(description="Playlist title")
    kind: str = Field(description="'personal' or 'youtube'")
    video_count: int | None = Field(
        None, description="Number of videos (list results only)"
    )


class ListLucarneChannelsResponse(BaseResponse):
    """Response for listing channels."""

    results: list[LucarneChannel] = Field(description="Channels")
    total_count: int = Field(description="Number of channels returned")


class ListLucarneCatalogsResponse(BaseResponse):
    """Response for listing catalogues."""

    results: list[LucarneCatalog] = Field(description="Catalogues")
    total_count: int = Field(description="Number of catalogues returned")


class LucarneCatalogResponse(BaseResponse):
    """Response for a single catalogue."""

    catalog: LucarneCatalog = Field(description="The catalogue")


class DeleteLucarneCatalogResponse(BaseResponse):
    """Response for deleting a catalogue."""

    deleted_id: int = Field(description="ID of the deleted catalogue")


class ListLucarnePlaylistsResponse(BaseResponse):
    """Response for listing playlists."""

    results: list[LucarnePlaylist] = Field(description="Playlists")
    total_count: int = Field(description="Number of playlists returned")


class LucarnePlaylistResponse(BaseResponse):
    """Response for a single playlist."""

    playlist: LucarnePlaylist = Field(description="The playlist")


class DeleteLucarnePlaylistResponse(BaseResponse):
    """Response for deleting a playlist."""

    playlist_id: int = Field(description="ID of the playlist being deleted")
    queued: bool = Field(
        description="True: Lucarne deletes it asynchronously, not yet gone"
    )
