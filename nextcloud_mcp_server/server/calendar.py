import datetime as dt
import logging
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.calendar import (
    Calendar,
    ListCalendarsResponse,
    ListTodosResponse,
    Todo,
)

logger = logging.getLogger(__name__)


def configure_calendar_tools(mcp: FastMCP):
    # Calendar tools
    @mcp.tool()
    @require_scopes("calendar:read")
    async def nc_calendar_list_calendars(ctx: Context) -> ListCalendarsResponse:
        """List all available calendars for the user"""
        client = await get_client(ctx)
        calendars_data = await client.calendar.list_calendars()

        calendars = [Calendar(**cal_data) for cal_data in calendars_data]
        return ListCalendarsResponse(calendars=calendars, total_count=len(calendars))

    @mcp.tool()
    @require_scopes("calendar:write")
    async def nc_calendar_create_event(
        calendar_name: str,
        title: str,
        start_datetime: str,
        ctx: Context,
        end_datetime: str = "",
        all_day: bool = False,
        description: str = "",
        location: str = "",
        categories: str = "",
        recurring: bool = False,
        recurrence_rule: str = "",
        recurrence_end_date: str = "",
        reminder_minutes: int = 15,
        reminder_email: bool = False,
        status: str = "CONFIRMED",
        priority: int = 5,
        privacy: str = "PUBLIC",
        attendees: str = "",
        url: str = "",
        color: str = "",
    ):
        """Create a comprehensive calendar event with full feature support

        Args:
            calendar_name: Name of the calendar to create the event in
            title: Event title
            start_datetime: ISO format: "2025-01-15T14:00:00" or "2025-01-15" for all-day
            ctx: MCP context
            end_datetime: ISO format end time, empty for all-day events
            all_day: Whether this is an all-day event
            description: Event description/details
            location: Event location
            categories: Comma-separated categories (e.g., "work,meeting")
            recurring: Whether this is a recurring event
            recurrence_rule: RFC5545 RRULE (e.g., "FREQ=WEEKLY;BYDAY=MO,WE,FR")
            recurrence_end_date: When to stop recurring
            reminder_minutes: Minutes before event to send reminder
            reminder_email: Whether to send email notification
            status: Event status: CONFIRMED, TENTATIVE, or CANCELLED
            priority: Priority level 1-9 (1=highest, 9=lowest, 5=normal)
            privacy: Privacy level: PUBLIC, PRIVATE, or CONFIDENTIAL
            attendees: Comma-separated email addresses
            url: Related URL for the event
            color: Event color (hex or name)

        Returns:
            Dict with event creation result
        """
        client = await get_client(ctx)

        event_data = {
            "title": title,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "all_day": all_day,
            "description": description,
            "location": location,
            "categories": categories,
            "recurring": recurring,
            "recurrence_rule": recurrence_rule,
            "recurrence_end_date": recurrence_end_date,
            "reminder_minutes": reminder_minutes,
            "reminder_email": reminder_email,
            "status": status,
            "priority": priority,
            "privacy": privacy,
            "attendees": attendees,
            "url": url,
            "color": color,
        }

        return await client.calendar.create_event(calendar_name, event_data)

    @mcp.tool()
    @require_scopes("calendar:read")
    async def nc_calendar_list_events(
        calendar_name: str,
        ctx: Context,
        start_date: str = "",
        end_date: str = "",
        limit: int = 50,
        min_attendees: Optional[int] = None,
        min_duration_minutes: Optional[int] = None,
        categories: Optional[str] = None,
        status: Optional[str] = None,
        title_contains: Optional[str] = None,
        location_contains: Optional[str] = None,
        search_all_calendars: bool = False,
    ):
        """List events in a calendar (or all calendars) within date range with advanced filtering.

        Args:
            calendar_name: Name of the calendar to search. Ignored if search_all_calendars=True.
            ctx: MCP context
            start_date: Start date for search (YYYY-MM-DD format, e.g., "2025-01-01")
            end_date: End date for search (YYYY-MM-DD format, e.g., "2025-01-31")
            limit: Maximum number of events to return
            min_attendees: Filter events with at least this many attendees
            min_duration_minutes: Filter events with at least this duration
            categories: Filter events containing any of these categories (comma-separated, e.g., "work,meeting")
            status: Filter events by status (CONFIRMED, TENTATIVE, or CANCELLED)
            title_contains: Filter events where title contains this text
            location_contains: Filter events where location contains this text
            search_all_calendars: If True, search across all calendars instead of just one

        Returns:
            List of events matching the filters
        """
        client = await get_client(ctx)

        # Convert YYYY-MM-DD format dates to datetime objects
        start_datetime = None
        end_datetime = None

        if start_date:
            try:
                start_datetime = dt.datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                # If parsing fails, try to parse as ISO format
                try:
                    start_datetime = dt.datetime.fromisoformat(start_date)
                except ValueError:
                    logger.warning(f"Invalid start_date format: {start_date}")

        if end_date:
            try:
                # For end date, set to end of day (23:59:59)
                end_datetime = dt.datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
            except ValueError:
                # If parsing fails, try to parse as ISO format
                try:
                    end_datetime = dt.datetime.fromisoformat(end_date)
                except ValueError:
                    logger.warning(f"Invalid end_date format: {end_date}")

        # Build filters dictionary
        filters = {}
        if min_attendees is not None:
            filters["min_attendees"] = min_attendees
        if min_duration_minutes is not None:
            filters["min_duration_minutes"] = min_duration_minutes
        if categories is not None:
            filters["categories"] = [cat.strip() for cat in categories.split(",")]
        if status is not None:
            filters["status"] = status
        if title_contains is not None:
            filters["title_contains"] = title_contains
        if location_contains is not None:
            filters["location_contains"] = location_contains

        if search_all_calendars:
            # Search across all calendars with filters
            events = await client.calendar.search_events_across_calendars(
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                filters=filters if filters else None,
            )
            return events[:limit]
        else:
            # Search in specific calendar
            events = await client.calendar.get_calendar_events(
                calendar_name=calendar_name,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                limit=limit,
            )

            # Apply filters if provided
            if filters:
                events = client.calendar._apply_event_filters(events, filters)

            return events

    @mcp.tool()
    @require_scopes("calendar:read")
    async def nc_calendar_get_event(
        calendar_name: str,
        event_uid: str,
        ctx: Context,
    ):
        """Get detailed information about a specific event"""
        client = await get_client(ctx)
        event_data, etag = await client.calendar.get_event(calendar_name, event_uid)
        return event_data

    @mcp.tool()
    @require_scopes("calendar:write")
    async def nc_calendar_update_event(
        calendar_name: str,
        event_uid: str,
        ctx: Context,
        # All the same parameters as create_event but optional
        title: str | None = None,
        start_datetime: str | None = None,
        end_datetime: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        location: str | None = None,
        categories: str | None = None,
        # Recurrence updates
        recurring: bool | None = None,
        recurrence_rule: str | None = None,
        # Notification updates
        reminder_minutes: int | None = None,
        reminder_email: bool | None = None,
        # Event property updates
        status: str | None = None,
        priority: int | None = None,
        privacy: str | None = None,
        attendees: str | None = None,
        url: str | None = None,
        color: str | None = None,
        etag: str = "",
    ):
        """Update any aspect of an existing event"""
        client = await get_client(ctx)

        # Build update data with only non-None values
        event_data = {}
        if title is not None:
            event_data["title"] = title
        if start_datetime is not None:
            event_data["start_datetime"] = start_datetime
        if end_datetime is not None:
            event_data["end_datetime"] = end_datetime
        if all_day is not None:
            event_data["all_day"] = all_day
        if description is not None:
            event_data["description"] = description
        if location is not None:
            event_data["location"] = location
        if categories is not None:
            event_data["categories"] = categories
        if recurring is not None:
            event_data["recurring"] = recurring
        if recurrence_rule is not None:
            event_data["recurrence_rule"] = recurrence_rule
        if reminder_minutes is not None:
            event_data["reminder_minutes"] = reminder_minutes
        if reminder_email is not None:
            event_data["reminder_email"] = reminder_email
        if status is not None:
            event_data["status"] = status
        if priority is not None:
            event_data["priority"] = priority
        if privacy is not None:
            event_data["privacy"] = privacy
        if attendees is not None:
            event_data["attendees"] = attendees
        if url is not None:
            event_data["url"] = url
        if color is not None:
            event_data["color"] = color

        return await client.calendar.update_event(
            calendar_name, event_uid, event_data, etag
        )

    @mcp.tool()
    @require_scopes("calendar:write")
    async def nc_calendar_delete_event(
        calendar_name: str,
        event_uid: str,
        ctx: Context,
    ):
        """Delete a calendar event"""
        client = await get_client(ctx)
        return await client.calendar.delete_event(calendar_name, event_uid)

    @mcp.tool()
    @require_scopes("calendar:write")
    async def nc_calendar_create_meeting(
        title: str,
        date: str,
        time: str,
        ctx: Context,
        duration_minutes: int = 60,
        calendar_name: str = "personal",
        attendees: str = "",
        location: str = "",
        description: str = "",
        reminder_minutes: int = 15,
    ):
        """Quick meeting creation with smart defaults

        This is a convenience function for creating events with common meeting defaults.
        It automatically:
        - Calculates end time based on duration
        - Sets status to CONFIRMED
        - Adds a reminder
        - Uses simpler date/time inputs instead of full ISO format

        For full control over all event properties, use nc_calendar_create_event instead.

        Args:
            title: Meeting title
            date: Meeting date (YYYY-MM-DD format, e.g., "2025-01-15")
            time: Meeting start time (HH:MM format, e.g., "14:00")
            ctx: MCP context
            duration_minutes: Meeting duration in minutes (default: 60)
            calendar_name: Calendar to create the meeting in (default: "personal")
            attendees: Comma-separated email addresses of attendees
            location: Meeting location
            description: Meeting description/agenda
            reminder_minutes: Minutes before meeting to send reminder (default: 15)

        Returns:
            Dict with meeting creation result
        """
        client = await get_client(ctx)

        # Combine date and time for start_datetime
        start_datetime = f"{date}T{time}:00"

        # Calculate end_datetime
        start_dt = dt.datetime.fromisoformat(start_datetime)
        end_dt = start_dt + dt.timedelta(minutes=duration_minutes)
        end_datetime = end_dt.isoformat()

        event_data = {
            "title": title,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "all_day": False,
            "description": description,
            "location": location,
            "attendees": attendees,
            "reminder_minutes": reminder_minutes,
            "status": "CONFIRMED",
            "priority": 5,
            "privacy": "PUBLIC",
        }

        return await client.calendar.create_event(calendar_name, event_data)

    @mcp.tool()
    @require_scopes("calendar:read")
    async def nc_calendar_get_upcoming_events(
        ctx: Context,
        calendar_name: str = "",  # Empty = all calendars
        days_ahead: int = 7,
        limit: int = 10,
    ):
        """Get upcoming events in next N days"""
        client = await get_client(ctx)

        now = dt.datetime.now()
        end_datetime = now + dt.timedelta(days=days_ahead)

        if calendar_name:
            # Get events from specific calendar
            return await client.calendar.get_calendar_events(
                calendar_name=calendar_name,
                start_datetime=now,
                end_datetime=end_datetime,
                limit=limit,
            )
        else:
            # Get events from all calendars
            all_calendars = await client.calendar.list_calendars()
            all_events = []

            for calendar in all_calendars:
                try:
                    events = await client.calendar.get_calendar_events(
                        calendar_name=calendar["name"],
                        start_datetime=now,
                        end_datetime=end_datetime,
                        limit=limit,
                    )
                    # Add calendar info to each event
                    for event in events:
                        event["calendar_name"] = calendar["name"]
                        event["calendar_display_name"] = calendar["display_name"]
                    all_events.extend(events)
                except Exception as e:
                    logger.warning(
                        f"Error getting events from calendar {calendar['name']}: {e}"
                    )
                    continue

            # Sort by start time and limit
            all_events.sort(key=lambda x: x.get("start_datetime", ""))
            return all_events[:limit]

    @mcp.tool()
    @require_scopes("calendar:read")
    async def nc_calendar_find_availability(
        duration_minutes: int,
        ctx: Context,
        attendees: str = "",  # Comma-separated email list
        date_range_start: str = "",  # "2025-07-28"
        date_range_end: str = "",  # "2025-08-04"
        business_hours_only: bool = True,
        exclude_weekends: bool = True,
        preferred_times: str = "",  # Comma-separated time ranges like "09:00-12:00,14:00-17:00"
    ):
        """Find available time slots for scheduling meetings.

        This tool intelligently analyzes existing calendar events to find free time slots
        that work for all specified attendees within the given constraints.

        Args:
            duration_minutes: Required duration for the meeting in minutes
            attendees: Comma-separated list of attendee email addresses to check availability for
            date_range_start: Start date for availability search (YYYY-MM-DD)
            date_range_end: End date for availability search (YYYY-MM-DD)
            business_hours_only: Only suggest slots during business hours (9 AM - 5 PM)
            exclude_weekends: Skip weekends when finding availability
            preferred_times: Preferred time ranges as "HH:MM-HH:MM" (comma-separated)

        Returns:
            List of available time slots with start/end times and duration
        """
        client = await get_client(ctx)

        # Parse attendees
        attendee_list = []
        if attendees:
            attendee_list = [
                email.strip() for email in attendees.split(",") if email.strip()
            ]

        # Parse preferred times
        preferred_time_list = []
        if preferred_times:
            preferred_time_list = [
                time_range.strip()
                for time_range in preferred_times.split(",")
                if time_range.strip()
            ]

        # Convert date strings to datetime objects
        start_datetime = None
        end_datetime = None

        if date_range_start:
            try:
                start_datetime = dt.datetime.strptime(date_range_start, "%Y-%m-%d")
            except ValueError:
                logger.warning(f"Invalid date_range_start format: {date_range_start}")

        if date_range_end:
            try:
                end_datetime = dt.datetime.strptime(date_range_end, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
            except ValueError:
                logger.warning(f"Invalid date_range_end format: {date_range_end}")

        # Build constraints
        constraints = {
            "business_hours_only": business_hours_only,
            "exclude_weekends": exclude_weekends,
            "preferred_times": preferred_time_list,
        }

        return await client.calendar.find_availability(
            duration_minutes=duration_minutes,
            attendees=attendee_list,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            constraints=constraints,
        )

    @mcp.tool()
    @require_scopes("calendar:write")
    async def nc_calendar_bulk_operations(
        operation: str,  # "update", "delete", "move"
        ctx: Context,
        title_contains: Optional[str] = None,
        categories: Optional[str] = None,  # Comma-separated
        calendar_name: Optional[str] = None,
        start_date: str = "",  # "2025-07-01"
        end_date: str = "",  # "2025-07-31"
        status: Optional[str] = None,
        location_contains: Optional[str] = None,
        # Update operation parameters
        new_title: Optional[str] = None,
        new_description: Optional[str] = None,
        new_location: Optional[str] = None,
        new_categories: Optional[str] = None,
        new_priority: Optional[int] = None,
        new_reminder_minutes: Optional[int] = None,
        # Move operation parameters
        target_calendar: Optional[str] = None,
    ):
        """Perform bulk operations (update/delete) on events matching filter criteria.

        This tool allows you to efficiently modify or delete multiple events at once
        by applying filters to find matching events and then performing the specified operation.

        Args:
            operation: Type of operation - "update" or "delete"
            title_contains: Filter events where title contains this text
            categories: Filter events containing any of these categories (comma-separated)
            calendar_name: Filter events from this specific calendar
            start_date: Filter events starting from this date (YYYY-MM-DD)
            end_date: Filter events ending before this date (YYYY-MM-DD)
            status: Filter events by status (CONFIRMED, TENTATIVE, CANCELLED)
            location_contains: Filter events where location contains this text

            # For update operations:
            new_title: New title for matching events
            new_description: New description for matching events
            new_location: New location for matching events
            new_categories: New categories for matching events (comma-separated)
            new_priority: New priority for matching events (1-9, 5=normal)
            new_reminder_minutes: New reminder time in minutes before event

            # For move operations:
            target_calendar: Calendar to move events to (requires operation="move")

        Returns:
            Summary of operation results including counts and details
        """
        client = await get_client(ctx)

        if operation not in ["update", "delete", "move"]:
            raise ValueError("Operation must be 'update', 'delete', or 'move'")

        # Convert date strings to datetime objects
        start_datetime = None
        end_datetime = None

        if start_date:
            try:
                start_datetime = dt.datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                logger.warning(f"Invalid start_date format: {start_date}")

        if end_date:
            try:
                end_datetime = dt.datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59
                )
            except ValueError:
                logger.warning(f"Invalid end_date format: {end_date}")

        # Build filter criteria
        filter_criteria = {}
        if title_contains is not None:
            filter_criteria["title_contains"] = title_contains
        if categories is not None:
            filter_criteria["categories"] = [
                cat.strip() for cat in categories.split(",")
            ]
        if status is not None:
            filter_criteria["status"] = status
        if location_contains is not None:
            filter_criteria["location_contains"] = location_contains
        # Add datetime strings for client compatibility
        if start_date:
            filter_criteria["start_date"] = start_date
        if end_date:
            filter_criteria["end_date"] = end_date

        if operation == "delete":
            # Find matching events and delete them
            if calendar_name:
                events = await client.calendar.get_calendar_events(
                    calendar_name=calendar_name,
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                )
                if filter_criteria:
                    events = client.calendar._apply_event_filters(
                        events, filter_criteria
                    )
            else:
                events = await client.calendar.search_events_across_calendars(
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                    filters=filter_criteria,
                )

            deleted_count = 0
            failed_count = 0
            results = []

            for event in events:
                try:
                    await client.calendar.delete_event(
                        event.get("calendar_name", calendar_name), event["uid"]
                    )
                    deleted_count += 1
                    results.append(
                        {
                            "uid": event["uid"],
                            "status": "deleted",
                            "title": event.get("title", ""),
                        }
                    )
                except Exception as e:
                    failed_count += 1
                    results.append(
                        {
                            "uid": event["uid"],
                            "status": "failed",
                            "error": str(e),
                            "title": event.get("title", ""),
                        }
                    )

            return {
                "operation": "delete",
                "total_found": len(events),
                "deleted_count": deleted_count,
                "failed_count": failed_count,
                "results": results,
            }

        elif operation == "update":
            # Build update data
            update_data = {}
            if new_title is not None:
                update_data["title"] = new_title
            if new_description is not None:
                update_data["description"] = new_description
            if new_location is not None:
                update_data["location"] = new_location
            if new_categories is not None:
                update_data["categories"] = new_categories
            if new_priority is not None:
                update_data["priority"] = new_priority
            if new_reminder_minutes is not None:
                update_data["reminder_minutes"] = new_reminder_minutes

            if not update_data:
                raise ValueError("No update data provided for update operation")

            return await client.calendar.bulk_update_events(
                filter_criteria, update_data
            )

        elif operation == "move":
            if not target_calendar:
                raise ValueError("target_calendar is required for move operation")

            # Find matching events
            if calendar_name:
                events = await client.calendar.get_calendar_events(
                    calendar_name=calendar_name,
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                )
                if filter_criteria:
                    events = client.calendar._apply_event_filters(
                        events, filter_criteria
                    )
            else:
                events = await client.calendar.search_events_across_calendars(
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                    filters=filter_criteria,
                )

            moved_count = 0
            failed_count = 0
            results = []

            for event in events:
                try:
                    # Create event in target calendar
                    event_data = {
                        k: v
                        for k, v in event.items()
                        if k
                        not in [
                            "uid",
                            "href",
                            "etag",
                            "calendar_name",
                            "calendar_display_name",
                        ]
                    }

                    await client.calendar.create_event(target_calendar, event_data)

                    # Delete from source calendar
                    await client.calendar.delete_event(
                        event.get("calendar_name", calendar_name), event["uid"]
                    )

                    moved_count += 1
                    results.append(
                        {
                            "uid": event["uid"],
                            "status": "moved",
                            "title": event.get("title", ""),
                            "from_calendar": event.get("calendar_name", calendar_name),
                            "to_calendar": target_calendar,
                        }
                    )
                except Exception as e:
                    failed_count += 1
                    results.append(
                        {
                            "uid": event["uid"],
                            "status": "failed",
                            "error": str(e),
                            "title": event.get("title", ""),
                        }
                    )

            return {
                "operation": "move",
                "total_found": len(events),
                "moved_count": moved_count,
                "failed_count": failed_count,
                "target_calendar": target_calendar,
                "results": results,
            }

    @mcp.tool()
    @require_scopes("calendar:write")
    async def nc_calendar_manage_calendar(
        action: str,  # "create", "delete", "update", "list"
        ctx: Context,
        calendar_name: str = "",
        display_name: str = "",
        description: str = "",
        color: str = "#1976D2",  # Default blue color
    ):
        """Manage calendar creation, deletion, and properties.

        This tool provides comprehensive calendar management functionality including
        creating new calendars, deleting existing ones, and updating calendar properties.

        Args:
            action: Action to perform - "create", "delete", "update", or "list"
            calendar_name: Internal name for the calendar (required for create/delete/update)
            display_name: Human-readable name for the calendar (used for create/update)
            description: Description for the calendar (used for create/update)
            color: Hex color code for the calendar (e.g., "#1976D2" for blue)

        Returns:
            Result of the calendar management operation
        """
        client = await get_client(ctx)

        if action == "list":
            return await client.calendar.list_calendars()

        elif action == "create":
            if not calendar_name:
                raise ValueError("calendar_name is required for create action")

            return await client.calendar.create_calendar(
                calendar_name=calendar_name,
                display_name=display_name or calendar_name,
                description=description,
                color=color,
            )

        elif action == "delete":
            if not calendar_name:
                raise ValueError("calendar_name is required for delete action")

            return await client.calendar.delete_calendar(calendar_name)

        elif action == "update":
            if not calendar_name:
                raise ValueError("calendar_name is required for update action")

            # Note: Calendar property updates require additional CalDAV PROPPATCH implementation
            # For now, return an informative message
            return {
                "status": "not_implemented",
                "message": "Calendar property updates require PROPPATCH implementation",
                "calendar_name": calendar_name,
                "requested_changes": {
                    "display_name": display_name,
                    "description": description,
                    "color": color,
                },
            }

        else:
            raise ValueError("Action must be 'create', 'delete', 'update', or 'list'")

    # ============= Todo/Task Tools =============

    @mcp.tool()
    @require_scopes("todo:read", "calendar:read")
    async def nc_calendar_list_todos(
        calendar_name: str,
        ctx: Context,
        status: Optional[str] = None,
        min_priority: Optional[int] = None,
        categories: Optional[str] = None,
        summary_contains: Optional[str] = None,
    ) -> ListTodosResponse:
        """List todos/tasks in a calendar with optional filtering.

        Args:
            calendar_name: Name of the calendar to list todos from
            ctx: MCP context
            status: Filter by status (NEEDS-ACTION, IN-PROCESS, COMPLETED, CANCELLED)
            min_priority: Filter by minimum priority (1=highest, 9=lowest)
            categories: Filter by categories (comma-separated, e.g., "work,urgent")
            summary_contains: Filter todos where summary contains this text

        Returns:
            List of todos matching the filters
        """
        client = await get_client(ctx)

        # Build filters dictionary
        filters = {}
        if status is not None:
            filters["status"] = status
        if min_priority is not None:
            filters["min_priority"] = min_priority
        if categories is not None:
            filters["categories"] = [cat.strip() for cat in categories.split(",")]
        if summary_contains is not None:
            filters["summary_contains"] = summary_contains

        todos_data = await client.calendar.list_todos(
            calendar_name, filters if filters else None
        )

        todos = [Todo(**todo_data) for todo_data in todos_data]
        return ListTodosResponse(
            todos=todos, calendar_name=calendar_name, total_count=len(todos)
        )

    @mcp.tool()
    @require_scopes("todo:write", "calendar:read")
    async def nc_calendar_create_todo(
        calendar_name: str,
        summary: str,
        ctx: Context,
        description: str = "",
        status: str = "NEEDS-ACTION",
        priority: int = 0,
        due: str = "",
        dtstart: str = "",
        categories: str = "",
    ):
        """Create a new todo/task in a calendar.

        Args:
            calendar_name: Name of the calendar to create the todo in
            summary: Todo title/summary
            ctx: MCP context
            description: Detailed description of the todo
            status: Todo status (NEEDS-ACTION, IN-PROCESS, COMPLETED, CANCELLED)
            priority: Priority (0=undefined, 1=highest, 9=lowest)
            due: Due date/time (ISO format, e.g., "2025-01-15T14:00:00")
            dtstart: Start date/time (ISO format)
            categories: Comma-separated categories (e.g., "work,urgent")

        Returns:
            Dict with todo creation result
        """
        client = await get_client(ctx)

        todo_data = {
            "summary": summary,
            "description": description,
            "status": status,
            "priority": priority,
            "due": due,
            "dtstart": dtstart,
            "categories": categories,
        }

        return await client.calendar.create_todo(calendar_name, todo_data)

    @mcp.tool()
    @require_scopes("todo:write", "calendar:read")
    async def nc_calendar_update_todo(
        calendar_name: str,
        todo_uid: str,
        ctx: Context,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[int] = None,
        percent_complete: Optional[int] = None,
        due: Optional[str] = None,
        dtstart: Optional[str] = None,
        completed: Optional[str] = None,
        categories: Optional[str] = None,
    ):
        """Update an existing todo/task.

        Args:
            calendar_name: Name of the calendar containing the todo
            todo_uid: UID of the todo to update
            ctx: MCP context
            summary: New summary/title
            description: New description
            status: New status (NEEDS-ACTION, IN-PROCESS, COMPLETED, CANCELLED)
            priority: New priority (0-9)
            percent_complete: New completion percentage (0-100)
            due: New due date/time (ISO format)
            dtstart: New start date/time (ISO format)
            completed: Completion timestamp (ISO format)
            categories: New categories (comma-separated)

        Returns:
            Dict with todo update result
        """
        client = await get_client(ctx)

        # Build update data with only non-None values
        todo_data = {}
        if summary is not None:
            todo_data["summary"] = summary
        if description is not None:
            todo_data["description"] = description
        if status is not None:
            todo_data["status"] = status
        if priority is not None:
            todo_data["priority"] = priority
        if percent_complete is not None:
            todo_data["percent_complete"] = percent_complete
        if due is not None:
            todo_data["due"] = due
        if dtstart is not None:
            todo_data["dtstart"] = dtstart
        if completed is not None:
            todo_data["completed"] = completed
        if categories is not None:
            todo_data["categories"] = categories

        return await client.calendar.update_todo(calendar_name, todo_uid, todo_data)

    @mcp.tool()
    @require_scopes("todo:write", "calendar:read")
    async def nc_calendar_delete_todo(
        calendar_name: str,
        todo_uid: str,
        ctx: Context,
    ):
        """Delete a todo/task from a calendar.

        Args:
            calendar_name: Name of the calendar containing the todo
            todo_uid: UID of the todo to delete
            ctx: MCP context

        Returns:
            Dict with deletion status
        """
        client = await get_client(ctx)
        return await client.calendar.delete_todo(calendar_name, todo_uid)

    @mcp.tool()
    @require_scopes("todo:read", "calendar:read")
    async def nc_calendar_search_todos(
        ctx: Context,
        status: Optional[str] = None,
        min_priority: Optional[int] = None,
        categories: Optional[str] = None,
        summary_contains: Optional[str] = None,
    ):
        """Search todos across all calendars with optional filtering.

        Args:
            ctx: MCP context
            status: Filter by status (NEEDS-ACTION, IN-PROCESS, COMPLETED, CANCELLED)
            min_priority: Filter by minimum priority (1=highest, 9=lowest)
            categories: Filter by categories (comma-separated, e.g., "work,urgent")
            summary_contains: Filter todos where summary contains this text

        Returns:
            List of todos matching the filters from all calendars
        """
        client = await get_client(ctx)

        # Build filters dictionary
        filters = {}
        if status is not None:
            filters["status"] = status
        if min_priority is not None:
            filters["min_priority"] = min_priority
        if categories is not None:
            filters["categories"] = [cat.strip() for cat in categories.split(",")]
        if summary_contains is not None:
            filters["summary_contains"] = summary_contains

        todos_data = await client.calendar.search_todos_across_calendars(
            filters if filters else None
        )

        todos = [Todo(**todo_data) for todo_data in todos_data]
        return ListTodosResponse(todos=todos, total_count=len(todos))
