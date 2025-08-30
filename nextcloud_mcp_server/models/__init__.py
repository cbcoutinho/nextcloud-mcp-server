"""Pydantic models for structured MCP server responses."""

# Base models
from .base import (
    BaseResponse,
    ErrorResponse,
    SuccessResponse,
    IdResponse,
    StatusResponse,
)

# Notes models
from .notes import (
    Note,
    NoteSearchResult,
    NotesSettings,
    CreateNoteResponse,
    UpdateNoteResponse,
    DeleteNoteResponse,
    AppendContentResponse,
    SearchNotesResponse,
)

# Calendar models
from .calendar import (
    Calendar,
    CalendarEvent,
    CalendarEventSummary,
    CreateEventResponse,
    UpdateEventResponse,
    DeleteEventResponse,
    ListEventsResponse,
    ListCalendarsResponse,
    AvailabilitySlot,
    FindAvailabilityResponse,
    BulkOperationResult,
    BulkOperationResponse,
    CreateMeetingResponse,
    UpcomingEventsResponse,
    ManageCalendarResponse,
)

# Contacts models
from .contacts import (
    AddressBook,
    Contact,
    ContactField,
    ListAddressBooksResponse,
    ListContactsResponse,
    CreateContactResponse,
    UpdateContactResponse,
    DeleteContactResponse,
    CreateAddressBookResponse,
    DeleteAddressBookResponse,
)

# Tables models
from .tables import (
    Table,
    TableColumn,
    TableRow,
    TableView,
    TableSchema,
    ListTablesResponse,
    GetSchemaResponse,
    ReadTableResponse,
    CreateRowResponse,
    UpdateRowResponse,
    DeleteRowResponse,
)

# WebDAV models
from .webdav import (
    FileInfo,
    DirectoryListing,
    ReadFileResponse,
    WriteFileResponse,
    CreateDirectoryResponse,
    DeleteResourceResponse,
)

__all__ = [
    # Base models
    "BaseResponse",
    "ErrorResponse",
    "SuccessResponse",
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
    "CreateEventResponse",
    "UpdateEventResponse",
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
]
