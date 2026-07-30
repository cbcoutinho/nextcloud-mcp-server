# Calendar App

### Calendar Tools

| Tool | Description |
|------|-------------|
| `nc_calendar_list_calendars` | List all available calendars for the user |
| `nc_calendar_create_event` | Create a comprehensive calendar event with full feature support (recurring, reminders, attendees, etc.) |
| `nc_calendar_list_events` | **Enhanced:** List events with advanced filtering (min attendees, duration, categories, status, search across all calendars). `calendar_name` is optional when `search_all_calendars=True` |
| `nc_calendar_get_event` | Get detailed information about a specific event |
| `nc_calendar_update_event` | Update any aspect of an existing event |
| `nc_calendar_delete_event` | Delete a calendar event |
| `nc_calendar_create_meeting` | Quick meeting creation with smart defaults |
| `nc_calendar_get_upcoming_events` | Get upcoming events in the next N days |
| `nc_calendar_find_availability` | **New:** Intelligent availability finder - find free time slots for meetings with attendee conflict detection |
| `nc_calendar_bulk_operations` | **New:** Bulk update, delete, or move events matching filter criteria |
| `nc_calendar_manage_calendar` | **New:** Create, delete, and manage calendar properties |
| `nc_calendar_complete_todo` | **New:** Mark a todo complete, setting `STATUS`, `PERCENT-COMPLETE` and `COMPLETED` together |

### Calendar Integration

The server provides comprehensive calendar integration through CalDAV, enabling you to:

- List all available calendars, including external read-only subscriptions
- Create, read, update, and delete calendar events  
- Handle recurring events with RRULE support
- Manage event reminders and notifications
- Support all-day and timed events
- Handle attendees and meeting invitations
- Organize events with categories and priorities

**Usage Examples:**

```python
# List available calendars. External subscriptions (webcal/ICS feeds) are
# included and reported with read_only=True and a `source` URL pointing at the
# upstream feed. Their events are readable through the normal event tools, but
# attempts to modify them will be rejected by Nextcloud.
calendars = await nc_calendar_list_calendars()

# Create a simple event
await nc_calendar_create_event(
    calendar_name="personal",
    title="Team Meeting", 
    start_datetime="2025-07-28T14:00:00",
    end_datetime="2025-07-28T15:00:00",
    description="Weekly team sync",
    location="Conference Room A"
)

# Create a recurring weekly meeting
await nc_calendar_create_event(
    calendar_name="work",
    title="Weekly Standup",
    start_datetime="2025-07-28T09:00:00", 
    end_datetime="2025-07-28T09:30:00",
    recurring=True,
    recurrence_rule="FREQ=WEEKLY;BYDAY=MO"
)

# Quick meeting creation
await nc_calendar_create_meeting(
    title="Client Call",
    date="2025-07-28",
    time="15:00",
    duration_minutes=60,
    attendees="client@example.com,colleague@company.com"
)

# Get upcoming events  
events = await nc_calendar_get_upcoming_events(days_ahead=7)

# Advanced search - find all meetings with 5+ attendees lasting 2+ hours
long_meetings = await nc_calendar_list_events(
    calendar_name="",  # Search all calendars
    search_all_calendars=True,
    start_date="2025-07-01",
    end_date="2025-07-31", 
    min_attendees=5,
    min_duration_minutes=120,
    title_contains="meeting"
)

# Find availability for a 1-hour meeting with specific attendees
availability = await nc_calendar_find_availability(
    duration_minutes=60,
    attendees="sarah@company.com,mike@company.com",
    date_range_start="2025-07-28",
    date_range_end="2025-08-04",
    business_hours_only=True,
    exclude_weekends=True,
    preferred_times="09:00-12:00,14:00-17:00"
)

# Bulk update all team meetings to new location
bulk_result = await nc_calendar_bulk_operations(
    operation="update",
    title_contains="team meeting",
    start_date="2025-08-01", 
    end_date="2025-08-31",
    new_location="Conference Room B",
    new_reminder_minutes=15
)

# Create a new project calendar
new_calendar = await nc_calendar_manage_calendar(
    action="create",
    calendar_name="project-alpha",
    display_name="Project Alpha Calendar",
    description="Calendar for Project Alpha team",
    color="#FF5722"
)
```

## Completing a todo

`nc_calendar_complete_todo` exists because RFC 5545 treats `STATUS`,
`PERCENT-COMPLETE` and `COMPLETED` as independent properties. Setting only
`status="COMPLETED"` via `nc_calendar_update_todo` leaves `PERCENT-COMPLETE` at
its previous value and writes no completion timestamp, so clients that surface
progress or completion dates disagree about whether the task is done.

```python
# Sets all three properties; `completed` defaults to now (UTC).
await nc_calendar_complete_todo(
    calendar_name="Personal",
    todo_uid="abc-123",
)

# Backdate the completion.
await nc_calendar_complete_todo(
    calendar_name="Personal",
    todo_uid="abc-123",
    completed="2026-01-01T09:00:00+00:00",
)
```

Not idempotent: a second call without an explicit `completed` restamps the
timestamp.

## Recurring todos

CalDAV does not expand VTODO recurrences: a `calendar-query` returns only the
master component, whose `DTSTART`/`DUE` describe the **first** instance of the
series. A chore created in 2023 that repeats every June therefore keeps
reporting `due: "2023-06-15"` forever, which reads as "three years overdue" even
though the current instance ran a few weeks ago.

`nc_calendar_list_todos` and `nc_calendar_search_todos` expand the recurrence
set client-side and add four fields to recurring todos:

| Field | Meaning |
|-------|---------|
| `recurring` | `true` when the todo has an `RRULE` |
| `recurrence_rule` | The RFC 5545 rule, e.g. `FREQ=YEARLY` |
| `current_dtstart` | Start of the currently relevant occurrence |
| `current_due` | Due date of the currently relevant occurrence |

"Currently relevant" is the most recent occurrence that has already started, or
the first upcoming one if the series has not started yet.

`dtstart`/`due` are deliberately left as stored, so updates keep addressing the
series rather than a single instance. **When judging whether a recurring todo is
overdue, read `current_due`, not `due`.** If the occurrence cannot be resolved
(no `DTSTART` to anchor the rule, or an unexpandable rule) the `current_*`
fields are omitted rather than guessed.
