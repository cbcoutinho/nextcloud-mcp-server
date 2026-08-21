"""Pydantic models for structured MCP server responses."""

# Base models
from .base import BaseResponse, IdResponse, StatusResponse

# Calendar models
from .calendar import (
    AvailabilitySlot,
    BulkOperationResponse,
    BulkOperationResult,
    Calendar,
    CalendarEvent,
    CalendarEventSummary,
    CompleteTodoResponse,
    CreateEventResponse,
    CreateMeetingResponse,
    DeleteEventResponse,
    FindAvailabilityResponse,
    ListCalendarsResponse,
    ListEventsResponse,
    ManageCalendarResponse,
    UpcomingEventsResponse,
    UpdateEventResponse,
    UpdateTodoResponse,
)

# Contacts models
from .contacts import (
    AddressBook,
    Contact,
    ContactField,
    CreateAddressBookResponse,
    CreateContactResponse,
    DeleteAddressBookResponse,
    DeleteContactResponse,
    ListAddressBooksResponse,
    ListContactsResponse,
    UpdateContactResponse,
)

# Notes models
from .notes import (
    AppendContentResponse,
    CreateNoteResponse,
    DeleteNoteResponse,
    Note,
    NoteSearchResult,
    NotesSettings,
    SearchNotesResponse,
    UpdateNoteResponse,
)

# Sharing models
from .sharing import (
    PublicDownloadLinkResponse,
    ShareType,
)

# Tables models
from .tables import (
    CreateRowResponse,
    DeleteRowResponse,
    GetSchemaResponse,
    ListTablesResponse,
    ReadTableResponse,
    Table,
    TableColumn,
    TableRow,
    TableSchema,
    TableView,
    UpdateRowResponse,
)

# WebDAV models
from .webdav import (
    CopyResourceResponse,
    CreateDirectoryResponse,
    CreateFileCommentResponse,
    DeleteResourceResponse,
    DirectoryListing,
    FileComment,
    FileInfo,
    ListFileCommentsResponse,
    MoveResourceResponse,
    ReadFileResponse,
    SearchFilesResponse,
    WriteFileResponse,
)

__all__ = [
    # Base models
    "BaseResponse",
    "IdResponse",
    "StatusResponse",
    # Notes models
    "Note",
    "NoteSearchResult",
    "NotesSettings",
    "CreateNoteResponse",
    "UpdateNoteResponse",
    "DeleteNoteResponse",
    "AppendContentResponse",
    "SearchNotesResponse",
    # Calendar models
    "Calendar",
    "CalendarEvent",
    "CalendarEventSummary",
    "CompleteTodoResponse",
    "CreateEventResponse",
    "UpdateEventResponse",
    "UpdateTodoResponse",
    "DeleteEventResponse",
    "ListEventsResponse",
    "ListCalendarsResponse",
    "AvailabilitySlot",
    "FindAvailabilityResponse",
    "BulkOperationResult",
    "BulkOperationResponse",
    "CreateMeetingResponse",
    "UpcomingEventsResponse",
    "ManageCalendarResponse",
    # Contacts models
    "AddressBook",
    "Contact",
    "ContactField",
    "ListAddressBooksResponse",
    "ListContactsResponse",
    "CreateContactResponse",
    "UpdateContactResponse",
    "DeleteContactResponse",
    "CreateAddressBookResponse",
    "DeleteAddressBookResponse",
    # Sharing models
    "PublicDownloadLinkResponse",
    "ShareType",
    # Tables models
    "Table",
    "TableColumn",
    "TableRow",
    "TableView",
    "TableSchema",
    "ListTablesResponse",
    "GetSchemaResponse",
    "ReadTableResponse",
    "CreateRowResponse",
    "UpdateRowResponse",
    "DeleteRowResponse",
    # WebDAV models
    "FileInfo",
    "DirectoryListing",
    "ReadFileResponse",
    "WriteFileResponse",
    "CreateDirectoryResponse",
    "DeleteResourceResponse",
    "MoveResourceResponse",
    "CopyResourceResponse",
    "SearchFilesResponse",
    "FileComment",
    "ListFileCommentsResponse",
    "CreateFileCommentResponse",
]
