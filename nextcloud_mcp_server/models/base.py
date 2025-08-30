"""Base Pydantic models for common response patterns."""

from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar, Union

from pydantic import BaseModel, Field


class BaseResponse(BaseModel):
    """Base response model for all MCP tool responses."""

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}

    success: bool = Field(
        default=True, description="Whether the operation was successful"
    )
    timestamp: datetime = Field(
        default_factory=datetime.now, description="Response timestamp"
    )


class ErrorResponse(BaseResponse):
    """Response model for error cases."""

    success: bool = Field(default=False, description="Always False for error responses")
    error: str = Field(description="Error message")
    error_code: Optional[str] = Field(None, description="Optional error code")
    details: Optional[Dict[str, Any]] = Field(
        None, description="Additional error details"
    )


class SuccessResponse(BaseResponse):
    """Generic success response."""

    message: Optional[str] = Field(None, description="Optional success message")
    data: Optional[Dict[str, Any]] = Field(None, description="Optional response data")


T = TypeVar("T")


class PaginatedResponse(BaseResponse, Generic[T]):
    """Generic paginated response model."""

    items: List[T] = Field(description="List of items")
    total_count: Optional[int] = Field(
        None, description="Total number of items available"
    )
    page: Optional[int] = Field(None, description="Current page number")
    page_size: Optional[int] = Field(None, description="Number of items per page")
    has_more: Optional[bool] = Field(
        None, description="Whether more items are available"
    )
    cursor: Optional[str] = Field(
        None, description="Cursor for next page (if using cursor-based pagination)"
    )


class IdResponse(BaseResponse):
    """Response model for operations that return a new ID."""

    id: Union[int, str] = Field(description="ID of the created or affected resource")


class StatusResponse(BaseResponse):
    """Response model for operations that return just a status."""

    status_code: Optional[int] = Field(None, description="HTTP status code")
    message: Optional[str] = Field(None, description="Status message")
