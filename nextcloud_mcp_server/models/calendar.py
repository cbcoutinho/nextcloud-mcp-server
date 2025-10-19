"""Pydantic models for Calendar app responses."""

from typing import List, Optional

from pydantic import BaseModel, Field

from .base import BaseResponse, StatusResponse


class Calendar(BaseModel):
    """Model for a Nextcloud calendar."""

    name: str = Field(description="Calendar name/ID")
    display_name: str = Field(description="Calendar display name")
    description: Optional[str] = Field(None, description="Calendar description")
    color: Optional[str] = Field(None, description="Calendar color")
    href: Optional[str] = Field(None, description="Calendar DAV href")
    timezone: Optional[str] = Field(None, description="Calendar timezone")
    enabled: bool = Field(default=True, description="Whether calendar is enabled")
    ctag: Optional[str] = Field(None, description="Calendar tag for synchronization")


class CalendarEventSummary(BaseModel):
    """Model for calendar event summary (for lists)."""

    uid: str = Field(description="Event UID")
    summary: str = Field(description="Event summary/title")
    start: str = Field(description="Event start datetime (ISO format)")
    end: Optional[str] = Field(None, description="Event end datetime (ISO format)")
    all_day: bool = Field(default=False, description="Whether event is all-day")
    location: Optional[str] = Field(None, description="Event location")
    description: Optional[str] = Field(None, description="Event description")
    categories: List[str] = Field(default_factory=list, description="Event categories")
    status: Optional[str] = Field(
        None, description="Event status (CONFIRMED, TENTATIVE, CANCELLED)"
    )


class CalendarEvent(CalendarEventSummary):
    """Model for a complete calendar event."""

    created: Optional[str] = Field(None, description="Event creation datetime")
    last_modified: Optional[str] = Field(None, description="Last modification datetime")
    recurring: bool = Field(default=False, description="Whether event is recurring")
    recurrence_rule: Optional[str] = Field(None, description="RFC5545 recurrence rule")
    recurrence_end: Optional[str] = Field(None, description="Recurrence end date")
    attendees: List[str] = Field(
        default_factory=list, description="List of attendee email addresses"
    )
    organizer: Optional[str] = Field(None, description="Event organizer")
    priority: Optional[int] = Field(None, description="Event priority (1-9)")
    privacy: Optional[str] = Field(None, description="Event privacy level")
    url: Optional[str] = Field(None, description="Event URL")
    duration_minutes: Optional[int] = Field(
        None, description="Event duration in minutes"
    )
    reminder_minutes: Optional[int] = Field(
        None, description="Reminder time in minutes before event"
    )
    reminder_email: bool = Field(
        default=False, description="Whether to send email reminder"
    )
    color: Optional[str] = Field(None, description="Event color")
    etag: Optional[str] = Field(None, description="ETag for versioning")


class CreateEventResponse(BaseResponse):
    """Response model for event creation."""

    event: CalendarEvent = Field(description="The created event")
    calendar_name: str = Field(
        description="Name of the calendar the event was created in"
    )


class UpdateEventResponse(BaseResponse):
    """Response model for event updates."""

    event: CalendarEvent = Field(description="The updated event")
    calendar_name: str = Field(description="Name of the calendar the event belongs to")


class DeleteEventResponse(StatusResponse):
    """Response model for event deletion."""

    deleted_uid: str = Field(description="UID of the deleted event")
    calendar_name: str = Field(
        description="Name of the calendar the event was deleted from"
    )


class ListEventsResponse(BaseResponse):
    """Response model for listing events."""

    events: List[CalendarEventSummary] = Field(description="List of events")
    calendar_name: Optional[str] = Field(
        None, description="Calendar name (if filtered to one calendar)"
    )
    start_date: Optional[str] = Field(None, description="Start date filter applied")
    end_date: Optional[str] = Field(None, description="End date filter applied")
    total_found: int = Field(description="Total number of events found")


class ListCalendarsResponse(BaseResponse):
    """Response model for listing calendars."""

    calendars: List[Calendar] = Field(description="List of available calendars")
    total_count: int = Field(description="Total number of calendars")


class AvailabilitySlot(BaseModel):
    """Model for an available time slot."""

    start: str = Field(description="Slot start datetime (ISO format)")
    end: str = Field(description="Slot end datetime (ISO format)")
    duration_minutes: int = Field(description="Slot duration in minutes")
    date: str = Field(description="Date of the slot (YYYY-MM-DD)")


class FindAvailabilityResponse(BaseResponse):
    """Response model for finding availability."""

    available_slots: List[AvailabilitySlot] = Field(
        description="List of available time slots"
    )
    duration_requested: int = Field(description="Requested duration in minutes")
    date_range_start: str = Field(description="Start date of search range")
    date_range_end: str = Field(description="End date of search range")
    attendees_checked: List[str] = Field(
        default_factory=list, description="Attendees checked for availability"
    )
    business_hours_only: bool = Field(
        description="Whether search was limited to business hours"
    )


class BulkOperationResult(BaseModel):
    """Model for bulk operation results."""

    operation: str = Field(description="Operation performed (update, delete, move)")
    events_processed: int = Field(description="Number of events processed")
    events_successful: int = Field(
        description="Number of events successfully processed"
    )
    events_failed: int = Field(description="Number of events that failed processing")
    failed_events: List[str] = Field(
        default_factory=list, description="UIDs of events that failed"
    )
    errors: List[str] = Field(default_factory=list, description="Error messages")


class BulkOperationResponse(BaseResponse):
    """Response model for bulk operations."""

    result: BulkOperationResult = Field(description="Bulk operation result")


class CreateMeetingResponse(CreateEventResponse):
    """Response model for meeting creation (same as event creation)."""

    pass


class UpcomingEventsResponse(BaseResponse):
    """Response model for upcoming events."""

    events: List[CalendarEventSummary] = Field(description="List of upcoming events")
    days_ahead: int = Field(description="Number of days ahead searched")
    calendar_name: Optional[str] = Field(
        None, description="Calendar name (if filtered to one calendar)"
    )


class ManageCalendarResponse(BaseResponse):
    """Response model for calendar management operations."""

    action: str = Field(description="Action performed (create, delete, update, list)")
    calendar: Optional[Calendar] = Field(None, description="Calendar that was affected")
    calendars: Optional[List[Calendar]] = Field(
        None, description="List of calendars (for list action)"
    )
    message: str = Field(description="Success message")


# ============= Todo/Task Models =============


class Todo(BaseModel):
    """Model for a CalDAV todo/task (VTODO)."""

    uid: str = Field(description="Todo UID")
    summary: str = Field(description="Todo summary/title")
    description: str = Field(default="", description="Todo description")
    status: str = Field(
        default="NEEDS-ACTION",
        description="Todo status: NEEDS-ACTION, IN-PROCESS, COMPLETED, CANCELLED",
    )
    priority: int = Field(
        default=0, description="Todo priority (0=undefined, 1=highest, 9=lowest)"
    )
    percent_complete: int = Field(default=0, description="Percentage complete (0-100)")
    due: Optional[str] = Field(None, description="Due date/time (ISO format)")
    dtstart: Optional[str] = Field(None, description="Start date/time (ISO format)")
    completed: Optional[str] = Field(
        None, description="Completion timestamp (ISO format)"
    )
    categories: str = Field(default="", description="Comma-separated categories")
    href: str = Field(default="", description="CalDAV href")
    etag: str = Field(default="", description="ETag for versioning")
    calendar_name: Optional[str] = Field(
        None, description="Calendar containing this todo"
    )
    calendar_display_name: Optional[str] = Field(
        None, description="Display name of calendar containing this todo"
    )


class ListTodosResponse(BaseResponse):
    """Response model for listing todos."""

    todos: List[Todo] = Field(description="List of todos/tasks")
    calendar_name: Optional[str] = Field(
        None, description="Calendar name (if filtered to one calendar)"
    )
    total_count: int = Field(description="Total number of todos found")


class CreateTodoResponse(BaseResponse):
    """Response model for todo creation."""

    todo: Todo = Field(description="The created todo")
    calendar_name: str = Field(
        description="Name of the calendar the todo was created in"
    )


class UpdateTodoResponse(BaseResponse):
    """Response model for todo updates."""

    todo: Todo = Field(description="The updated todo")
    calendar_name: str = Field(description="Name of the calendar the todo belongs to")


class DeleteTodoResponse(StatusResponse):
    """Response model for todo deletion."""

    deleted_uid: str = Field(description="UID of the deleted todo")
    calendar_name: str = Field(
        description="Name of the calendar the todo was deleted from"
    )
