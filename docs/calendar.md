# Calendar App

### Calendar Tools

| Tool | Description |
|------|-------------|
| `nc_calendar_list_calendars` | List all available calendars for the user |
| `nc_calendar_create_event` | Create a comprehensive calendar event with full feature support (recurring, reminders, attendees, etc.) |
| `nc_calendar_list_events` | **Enhanced:** List events with advanced filtering (min attendees, duration, categories, status, search across all calendars) |
| `nc_calendar_get_event` | Get detailed information about a specific event |
| `nc_calendar_update_event` | Update any aspect of an existing event |
| `nc_calendar_delete_event` | Delete a calendar event |
| `nc_calendar_create_meeting` | Quick meeting creation with smart defaults |
| `nc_calendar_get_upcoming_events` | Get upcoming events in the next N days |
| `nc_calendar_find_availability` | **New:** Intelligent availability finder - find free time slots for meetings with attendee conflict detection |
| `nc_calendar_bulk_operations` | **New:** Bulk update, delete, or move events matching filter criteria |
| `nc_calendar_manage_calendar` | **New:** Create, delete, and manage calendar properties |

### Calendar Integration

The server provides comprehensive calendar integration through CalDAV, enabling you to:

- List all available calendars
- Create, read, update, and delete calendar events  
- Handle recurring events with RRULE support
- Manage event reminders and notifications
- Support all-day and timed events
- Handle attendees and meeting invitations
- Organize events with categories and priorities

**Usage Examples:**

```python
# List available calendars
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
