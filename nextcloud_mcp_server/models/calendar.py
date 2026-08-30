"""Pydantic models for Calendar app responses."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .base import BaseResponse, StatusResponse


class Reminder(BaseModel):
    """A single VALARM on an event or todo.

    Exactly one trigger must be given. ``trigger_at`` fires at an absolute
    instant; ``minutes_before`` and ``trigger`` are relative to the component's
    start (or its end, via ``related``).
    """

    action: Literal["DISPLAY", "EMAIL", "AUDIO"] = Field(
        default="DISPLAY",
        description=(
            "Alarm type. Nextcloud silently discards any other value, so the "
            "set is closed rather than free-form."
        ),
    )
    minutes_before: Optional[int] = Field(
        None,
        ge=0,
        description=(
            "Fire this many minutes before the trigger point. Negative values "
            "are rejected rather than quietly flipped into an alarm *after* the "
            "event — use trigger with a positive duration for that."
        ),
    )
    trigger_at: Optional[str] = Field(
        None, description="Absolute ISO datetime to fire at"
    )
    trigger: Optional[str] = Field(
        None,
        description=(
            "Raw RFC 5545 duration, e.g. ``-PT30M`` or ``-P1D``. Use for offsets "
            "that minutes_before expresses awkwardly."
        ),
    )
    description: Optional[str] = Field(
        None,
        description=(
            "Alarm body, and the message text for an EMAIL alarm. Defaults to "
            "'Event reminder' or 'Todo reminder' depending on what it is "
            "attached to."
        ),
    )
    summary: Optional[str] = Field(
        None,
        description=(
            "Subject line of an EMAIL alarm. Defaults to the event/todo title. "
            "Ignored for other actions, which have no SUMMARY in RFC 5545."
        ),
    )
    related: Optional[Literal["START", "END"]] = Field(
        None,
        description=(
            "Whether a relative trigger counts from the start or the end. "
            "Only meaningful with minutes_before/trigger — RFC 5545 does not "
            "allow RELATED on an absolute trigger."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_trigger(self) -> "Reminder":
        given = [
            name
            for name in ("trigger_at", "trigger", "minutes_before")
            if getattr(self, name) is not None
        ]
        if len(given) != 1:
            raise ValueError(
                "a reminder needs exactly one of trigger_at, trigger or "
                f"minutes_before; got {given or 'none'}"
            )
        if self.related and self.trigger_at:
            raise ValueError("related cannot be combined with an absolute trigger_at")
        return self


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
    read_only: bool = Field(
        default=False,
        description="Whether the calendar is read-only (e.g. an external subscription)",
    )
    source: Optional[str] = Field(
        None,
        description="Source URL of an external/subscribed read-only calendar",
    )
    transparent: bool = Field(
        default=False,
        description=(
            "Whether the calendar is transparent for free/busy purposes "
            "(CalDAV schedule-calendar-transp, RFC 4791 5.2.9). Nextcloud "
            "surfaces this as 'never show me as busy'. Events in such a "
            "calendar should be ignored when computing availability."
        ),
    )


class CalendarEventSummary(BaseModel):
    """Model for calendar event summary (for lists)."""

    uid: str = Field(description="Event UID")
    summary: str = Field(description="Event summary/title")
    start: str = Field(
        description=(
            "Event start datetime (ISO format). No suffix = RFC 5545 floating "
            "local time; ``+00:00`` = UTC; an explicit offset (e.g. ``-04:00``) "
            "= TZID-bound at that instant. The IANA TZID name is exposed "
            "separately as ``start_tz``."
        )
    )
    end: Optional[str] = Field(
        None,
        description=(
            "Event end datetime (ISO format). Same encoding rules as ``start``."
        ),
    )
    start_tz: Optional[str] = Field(
        None,
        description=(
            "IANA timezone name when DTSTART had a TZID parameter (e.g. "
            "``America/New_York``). ``None`` for floating local or UTC."
        ),
    )
    end_tz: Optional[str] = Field(
        None, description="IANA timezone name when DTEND had a TZID parameter."
    )
    all_day: bool = Field(default=False, description="Whether event is all-day")
    location: Optional[str] = Field(None, description="Event location")
    description: Optional[str] = Field(None, description="Event description")
    categories: List[str] = Field(default_factory=list, description="Event categories")
    transp: Optional[str] = Field(
        None,
        description=(
            "Free/busy transparency of this event (RFC 5545 3.8.2.7 TRANSP): "
            "'OPAQUE' consumes time, 'TRANSPARENT' does not. Nextcloud shows "
            "this as 'Busy'/'Free' on the event."
        ),
    )
    status: Optional[str] = Field(
        None, description="Event status (CONFIRMED, TENTATIVE, CANCELLED)"
    )
    calendar_name: Optional[str] = Field(
        None, description="Calendar containing this event"
    )
    calendar_display_name: Optional[str] = Field(
        None, description="Display name of calendar containing this event"
    )


class CalendarEvent(CalendarEventSummary):
    """Model for a complete calendar event."""

    created: Optional[str] = Field(None, description="Event creation datetime")
    last_modified: Optional[str] = Field(None, description="Last modification datetime")
    recurring: bool = Field(default=False, description="Whether event is recurring")
    recurrence_rule: Optional[str] = Field(None, description="RFC5545 recurrence rule")
    recurrence_end_date: Optional[str] = Field(
        None,
        description=(
            "When the series stops recurring, read back from the rule's UNTIL. "
            "Named to match the write-side parameter so a value read from an "
            "event can be passed straight back into an update."
        ),
    )
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
    reminders: List[Reminder] = Field(
        default_factory=list,
        description="The event's VALARMs, in document order",
    )
    color: Optional[str] = Field(
        None,
        description=(
            "Event colour as a CSS3 colour name (RFC 7986 COLOR). Nextcloud's "
            "Calendar UI ignores hex values."
        ),
    )
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
    due: Optional[str] = Field(
        None,
        description=(
            "Due date/time (ISO format). A date-only value like '2026-08-08' "
            "means a whole-day task."
        ),
    )
    dtstart: Optional[str] = Field(
        None,
        description=("Start date/time (ISO format). Date-only means a whole-day task."),
    )
    completed: Optional[str] = Field(
        None, description="Completion timestamp (ISO format)"
    )
    categories: str = Field(default="", description="Comma-separated categories")
    recurring: bool = Field(
        default=False, description="Whether this todo recurs (i.e. has an RRULE)"
    )
    recurrence_rule: str = Field(
        default="", description="RFC 5545 RRULE value, e.g. 'FREQ=YEARLY;INTERVAL=1'"
    )
    reminders: List[Reminder] = Field(
        default_factory=list,
        description="The todo's VALARMs, in document order",
    )
    pending_count: Optional[int] = Field(
        None,
        description=(
            "Recurring todos only: how many occurrences have started and are "
            "not yet done. 0 means the series is up to date. Bounded by a "
            "three-year lookback, so treat it as a lower bound."
        ),
    )
    oldest_pending_dtstart: Optional[str] = Field(
        None,
        description=(
            "Recurring todos only: start of the oldest unfinished occurrence "
            "(ISO format) — how far the backlog reaches back."
        ),
    )
    oldest_pending_due: Optional[str] = Field(
        None,
        description=(
            "Recurring todos only: due date of the oldest unfinished "
            "occurrence (ISO format). Use this to say since when a recurring "
            "todo has been overdue."
        ),
    )
    current_dtstart: Optional[str] = Field(
        None,
        description=(
            "Recurring todos only: start of the most recent unfinished "
            "occurrence (ISO format). Prefer this over 'dtstart', which "
            "describes the first instance of the series and may be years old."
        ),
    )
    current_due: Optional[str] = Field(
        None,
        description=(
            "Recurring todos only: due date of the most recent unfinished "
            "occurrence (ISO format). Prefer this over 'due' when judging "
            "whether a recurring todo is overdue."
        ),
    )
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
    """Response model for todo updates.

    Carries identifiers and the post-write ETag rather than the updated
    ``Todo``: the CalDAV write returns exactly this much, and rebuilding a full
    todo would cost an extra read on every update. ``etag`` is the value to
    pass back into the next update to keep a read-modify-write cycle guarded.
    """

    uid: str = Field(description="UID of the updated todo")
    calendar_name: str = Field(description="Name of the calendar the todo belongs to")
    href: str = Field(default="", description="CalDAV href of the todo")
    etag: str = Field(
        default="",
        description=(
            "ETag after the write. Pass it as `etag` on the next update to "
            "detect a concurrent change; empty if the server sent none."
        ),
    )


class CompleteTodoResponse(BaseResponse):
    """Response model for marking a todo complete."""

    uid: str = Field(description="UID of the completed todo")
    calendar_name: str = Field(description="Calendar the todo belongs to")
    status: str = Field(
        default="COMPLETED", description="VTODO STATUS value that was written"
    )
    percent_complete: int = Field(
        default=100, description="PERCENT-COMPLETE value that was written"
    )
    completed: str = Field(description="ISO-8601 COMPLETED timestamp that was written")
    href: str = Field(default="", description="CalDAV href of the todo")
    etag: str = Field(
        default="",
        description=(
            "ETag after the write. Pass it as `etag` on the next update to "
            "detect a concurrent change; empty if the server sent none."
        ),
    )


class DeleteTodoResponse(StatusResponse):
    """Response model for todo deletion."""

    deleted_uid: str = Field(description="UID of the deleted todo")
    calendar_name: str = Field(
        description="Name of the calendar the todo was deleted from"
    )
