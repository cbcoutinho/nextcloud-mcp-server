"""Pydantic models for Tables app responses."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import BaseResponse, IdResponse, StatusResponse


class TableColumn(BaseModel):
    """Model for a table column definition."""

    id: int = Field(description="Column ID")
    title: str = Field(description="Column title")
    type: str = Field(description="Column type (text, number, datetime, etc.)")
    subtype: Optional[str] = Field(None, description="Column subtype")
    mandatory: bool = Field(default=False, description="Whether column is mandatory")
    description: Optional[str] = Field(None, description="Column description")
    text_default: Optional[str] = Field(None, description="Default text value")
    text_allowed_pattern: Optional[str] = Field(
        None, description="Allowed text pattern"
    )
    text_max_length: Optional[int] = Field(None, description="Maximum text length")
    number_default: Optional[float] = Field(None, description="Default number value")
    number_min: Optional[float] = Field(None, description="Minimum number value")
    number_max: Optional[float] = Field(None, description="Maximum number value")
    number_decimals: Optional[int] = Field(None, description="Number of decimal places")
    datetime_default: Optional[str] = Field(None, description="Default datetime value")
    selection_options: List[str] = Field(
        default_factory=list, description="Selection options"
    )
    selection_default: Optional[str] = Field(
        None, description="Default selection value"
    )


class TableRow(BaseModel):
    """Model for a table row."""

    id: int = Field(description="Row ID")
    created_by: Optional[str] = Field(None, description="User who created the row")
    created_at: Optional[str] = Field(None, description="Row creation timestamp")
    last_edit_by: Optional[str] = Field(
        None, description="User who last edited the row"
    )
    last_edit_at: Optional[str] = Field(None, description="Last edit timestamp")
    data: Dict[int, Any] = Field(description="Row data keyed by column ID")


class TableView(BaseModel):
    """Model for a table view."""

    id: int = Field(description="View ID")
    title: str = Field(description="View title")
    emoji: Optional[str] = Field(None, description="View emoji")
    description: Optional[str] = Field(None, description="View description")
    columns: List[int] = Field(
        default_factory=list, description="List of column IDs in this view"
    )
    sort: List[Dict[str, Any]] = Field(
        default_factory=list, description="Sort configuration"
    )
    filter: List[Dict[str, Any]] = Field(
        default_factory=list, description="Filter configuration"
    )


class Table(BaseModel):
    """Model for a Nextcloud table."""

    id: int = Field(description="Table ID")
    title: str = Field(description="Table title")
    emoji: Optional[str] = Field(None, description="Table emoji")
    ownership: str = Field(description="Table ownership")
    owner_display_name: str = Field(description="Display name of table owner")
    created_by: Optional[str] = Field(None, description="User who created the table")
    created_at: Optional[str] = Field(None, description="Table creation timestamp")
    last_edit_by: Optional[str] = Field(
        None, description="User who last edited the table"
    )
    last_edit_at: Optional[str] = Field(None, description="Last edit timestamp")
    row_count: int = Field(default=0, description="Number of rows in the table")
    has_shares: bool = Field(default=False, description="Whether table is shared")
    archived: bool = Field(default=False, description="Whether table is archived")
    is_shared: bool = Field(
        default=False, description="Whether table is shared with current user"
    )
    on_share_permissions: Optional[Dict[str, Any]] = Field(
        None, description="Share permissions"
    )


class TableSchema(BaseModel):
    """Model for complete table schema including columns and views."""

    table: Table = Field(description="Table information")
    columns: List[TableColumn] = Field(description="Table columns")
    views: List[TableView] = Field(description="Table views")


class ListTablesResponse(BaseResponse):
    """Response model for listing tables."""

    tables: List[Table] = Field(description="List of available tables")
    total_count: int = Field(description="Total number of tables")


class GetSchemaResponse(BaseResponse):
    """Response model for getting table schema."""

    table_schema: TableSchema = Field(description="Table schema information")


class ReadTableResponse(BaseResponse):
    """Response model for reading table rows."""

    rows: List[TableRow] = Field(description="Table rows")
    table_id: int = Field(description="Table ID")
    total_count: Optional[int] = Field(
        None, description="Total number of rows (if known)"
    )
    offset: Optional[int] = Field(None, description="Offset used for pagination")
    limit: Optional[int] = Field(None, description="Limit used for pagination")


class CreateRowResponse(IdResponse):
    """Response model for row creation."""

    row: TableRow = Field(description="The created row")
    table_id: int = Field(description="Table ID the row was created in")


class UpdateRowResponse(BaseResponse):
    """Response model for row updates."""

    row: TableRow = Field(description="The updated row")


class DeleteRowResponse(StatusResponse):
    """Response model for row deletion."""

    deleted_id: int = Field(description="ID of the deleted row")
