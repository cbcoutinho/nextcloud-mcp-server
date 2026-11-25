"""Pydantic models for Nextcloud Whiteboard responses."""

from nextcloud_client.models.whiteboard import WhiteboardElement
from pydantic import Field

from nextcloud_mcp_server.models.base import BaseResponse


class WhiteboardResponse(BaseResponse):
    """One board, as a page of its visible elements."""

    path: str = Field(description="Path of the board")
    etag: str | None = Field(
        description=(
            "Version of the file that was read. It changes on every save, so "
            "pages with different etags do not belong together."
        )
    )
    elements: list[WhiteboardElement] = Field(
        description="Visible elements, back to front, starting at `offset`"
    )
    total_count: int = Field(description="Visible elements on the whole board")
    offset: int = Field(description="Position of the first returned element")
    next_offset: int | None = Field(
        default=None,
        description="Pass as `offset` to read on. None when this page ends the board.",
    )
