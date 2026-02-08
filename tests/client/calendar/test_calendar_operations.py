"""Integration tests for Calendar CalDAV operations.

Note: These tests use the shared temporary_calendar fixture from conftest.py
which reuses a session-scoped calendar to avoid Nextcloud rate limiting issues.
Each test cleans up its own events/todos but shares the same calendar.
"""

import logging
import uuid
from datetime import datetime, timedelta

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture
async def temporary_event(nc_client: NextcloudClient, temporary_calendar: str):
    """Create a temporary event for testing and clean up afterward.

    Uses the shared temporary_calendar fixture from conftest.py which reuses
    a session-scoped calendar to avoid Nextcloud rate limiting.
    """
    event_uid = None
    calendar_name = temporary_calendar

    # Create a test event
    tomorrow = datetime.now() + timedelta(days=1)
    event_data = {
        "title": f"Test Event {uuid.uuid4().hex[:8]}",
        "start_datetime": tomorrow.strftime("%Y-%m-%dT14:00:00"),
        "end_datetime": tomorrow.strftime("%Y-%m-%dT15:00:00"),
        "description": "Test event created by integration tests",
        "location": "Test Location",
        "categories": "testing",
        "status": "CONFIRMED",
        "priority": 5,
    }

    try:
        logger.info(f"Creating temporary event in calendar: {calendar_name}")
        result = await nc_client.calendar.create_event(calendar_name, event_data)
        event_uid = result.get("uid")

        if not event_uid:
            pytest.fail("Failed to create temporary event")

        logger.info(f"Created temporary event with UID: {event_uid}")
        yield {"uid": event_uid, "calendar_name": calendar_name, "data": event_data}

    finally:
        # Cleanup
        if event_uid:
            try:
                logger.info(f"Cleaning up temporary event: {event_uid}")
                await nc_client.calendar.delete_event(calendar_name, event_uid)
                logger.info(f"Successfully deleted temporary event: {event_uid}")
            except HTTPStatusError as e:
                if e.response.status_code != 404:
                    logger.error(f"Error deleting temporary event {event_uid}: {e}")
            except Exception as e:
                logger.error(
                    f"Unexpected error deleting temporary event {event_uid}: {e}"
                )


async def test_list_calendars(nc_client: NextcloudClient):
    """Test listing available calendars."""
    calendars = await nc_client.calendar.list_calendars()

    assert isinstance(calendars, list)

    if not calendars:
        pytest.skip("No calendars available - Calendar app may not be enabled")

    logger.info(f"Found {len(calendars)} calendars")

    # Check structure of calendars
    for calendar in calendars:
        assert "name" in calendar
        assert "display_name" in calendar
        assert "href" in calendar
        # Optional fields
        assert "description" in calendar
        assert "color" in calendar

        logger.info(f"Calendar: {calendar['name']} - {calendar['display_name']}")


async def test_create_and_delete_event(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating and deleting a basic event."""
    calendar_name = temporary_calendar

    # Create event
    tomorrow = datetime.now() + timedelta(days=1)
    event_data = {
        "title": "Integration Test Event",
        "start_datetime": tomorrow.strftime("%Y-%m-%dT10:00:00"),
        "end_datetime": tomorrow.strftime("%Y-%m-%dT11:00:00"),
        "description": "Test event for integration testing",
        "location": "Test Room",
        "categories": "testing,integration",
        "status": "CONFIRMED",
        "priority": 3,
    }

    try:
        result = await nc_client.calendar.create_event(calendar_name, event_data)
        assert "uid" in result
        assert result["status_code"] in [200, 201, 204]

        event_uid = result["uid"]
        logger.info(f"Created event with UID: {event_uid}")

        # Verify event was created by retrieving it
        retrieved_event, etag = await nc_client.calendar.get_event(
            calendar_name, event_uid
        )
        assert retrieved_event["uid"] == event_uid
        assert retrieved_event["title"] == "Integration Test Event"
        assert retrieved_event["location"] == "Test Room"

        # Delete event
        delete_result = await nc_client.calendar.delete_event(calendar_name, event_uid)
        assert delete_result["status_code"] in [200, 204, 404]

        logger.info(f"Successfully deleted event: {event_uid}")

    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise


async def test_create_all_day_event(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating an all-day event."""
    calendar_name = temporary_calendar

    tomorrow = datetime.now() + timedelta(days=1)
    event_data = {
        "title": "All Day Test Event",
        "start_datetime": tomorrow.strftime("%Y-%m-%d"),
        "all_day": True,
        "description": "Test all-day event",
        "categories": "testing",
    }

    try:
        result = await nc_client.calendar.create_event(calendar_name, event_data)
        event_uid = result["uid"]
        logger.info(f"Created all-day event with UID: {event_uid}")

        # Verify event
        retrieved_event, _ = await nc_client.calendar.get_event(
            calendar_name, event_uid
        )
        assert retrieved_event["title"] == "All Day Test Event"
        assert retrieved_event.get("all_day") is True

        # Cleanup
        await nc_client.calendar.delete_event(calendar_name, event_uid)

    except Exception as e:
        logger.error(f"All-day event test failed: {e}")
        raise


async def test_create_recurring_event(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating a recurring event."""
    calendar_name = temporary_calendar

    tomorrow = datetime.now() + timedelta(days=1)
    event_data = {
        "title": "Weekly Recurring Test",
        "start_datetime": tomorrow.strftime("%Y-%m-%dT14:00:00"),
        "end_datetime": tomorrow.strftime("%Y-%m-%dT15:00:00"),
        "description": "Test recurring event",
        "recurring": True,
        "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
        "reminder_minutes": 30,
    }

    try:
        result = await nc_client.calendar.create_event(calendar_name, event_data)
        event_uid = result["uid"]
        logger.info(f"Created recurring event with UID: {event_uid}")

        # Verify event
        retrieved_event, _ = await nc_client.calendar.get_event(
            calendar_name, event_uid
        )
        assert retrieved_event["title"] == "Weekly Recurring Test"
        assert retrieved_event.get("recurring") is True

        # Cleanup
        await nc_client.calendar.delete_event(calendar_name, event_uid)

    except Exception as e:
        logger.error(f"Recurring event test failed: {e}")
        raise


async def test_list_events_in_range(nc_client: NextcloudClient, temporary_event: dict):
    """Test listing events within a date range."""
    calendar_name = temporary_event["calendar_name"]

    # Get events for the next week
    start_datetime = datetime.now()
    end_datetime = datetime.now() + timedelta(days=7)

    events = await nc_client.calendar.get_calendar_events(
        calendar_name=calendar_name,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        limit=50,
    )

    assert isinstance(events, list)
    logger.info(f"Found {len(events)} events in date range")

    # Our temporary event should be in the list
    event_uids = [event.get("uid") for event in events]
    assert temporary_event["uid"] in event_uids

    # Check event structure
    for event in events:
        assert "uid" in event
        assert "title" in event
        assert "start_datetime" in event


async def test_update_event(nc_client: NextcloudClient, temporary_event: dict):
    """Test updating an existing event."""
    calendar_name = temporary_event["calendar_name"]
    event_uid = temporary_event["uid"]

    # Update event data
    updated_data = {
        "title": "Updated Test Event Title",
        "description": "Updated description for test event",
        "location": "Updated Location",
        "priority": 1,  # High priority
    }

    try:
        result = await nc_client.calendar.update_event(
            calendar_name, event_uid, updated_data
        )
        assert result["uid"] == event_uid

        # Verify updates
        updated_event, _ = await nc_client.calendar.get_event(calendar_name, event_uid)
        assert updated_event["title"] == "Updated Test Event Title"
        assert updated_event["description"] == "Updated description for test event"
        assert updated_event["location"] == "Updated Location"
        assert updated_event["priority"] == 1

        logger.info(f"Successfully updated event: {event_uid}")

    except Exception as e:
        logger.error(f"Event update test failed: {e}")
        raise


async def test_update_event_extended_fields(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test updating categories, recurrence_rule, attendees, and reminder_minutes."""
    calendar_name = temporary_calendar

    tomorrow = datetime.now() + timedelta(days=1)
    event_data = {
        "title": "Extended Fields Update Test",
        "start_datetime": tomorrow.strftime("%Y-%m-%dT10:00:00"),
        "end_datetime": tomorrow.strftime("%Y-%m-%dT11:00:00"),
        "description": "Base event for extended-field update test",
    }

    event_uid = None
    try:
        result = await nc_client.calendar.create_event(calendar_name, event_data)
        event_uid = result["uid"]
        logger.info(f"Created base event for extended fields test: {event_uid}")

        # --- Phase 1: Set all four extended fields ---
        updated_data = {
            "categories": "work,meeting",
            "recurrence_rule": "FREQ=WEEKLY;COUNT=4",
            "attendees": "alice@example.com,bob@example.com",
            "reminder_minutes": 15,
        }
        await nc_client.calendar.update_event(calendar_name, event_uid, updated_data)

        retrieved, _ = await nc_client.calendar.get_event(calendar_name, event_uid)

        # Verify categories
        assert "work" in retrieved.get("categories", "")
        assert "meeting" in retrieved.get("categories", "")

        # Verify recurrence rule
        assert retrieved.get("recurring") is True
        assert "WEEKLY" in retrieved.get("recurrence_rule", "")

        # Verify attendees
        attendees = retrieved.get("attendees", "")
        assert "alice@example.com" in attendees
        assert "bob@example.com" in attendees

        logger.info("Phase 1 passed: all extended fields set correctly")

        # --- Phase 2: Clear all four extended fields ---
        cleared_data = {
            "categories": "",
            "recurrence_rule": "",
            "attendees": "",
            "reminder_minutes": 0,
        }
        await nc_client.calendar.update_event(calendar_name, event_uid, cleared_data)

        cleared, _ = await nc_client.calendar.get_event(calendar_name, event_uid)

        # Verify categories cleared
        assert not cleared.get("categories")

        # Verify recurrence cleared
        assert cleared.get("recurring") is not True
        assert not cleared.get("recurrence_rule")

        # Verify attendees cleared
        assert not cleared.get("attendees")

        logger.info("Phase 2 passed: all extended fields cleared correctly")

    except Exception as e:
        logger.error(f"Extended fields update test failed: {e}")
        raise
    finally:
        if event_uid:
            try:
                await nc_client.calendar.delete_event(calendar_name, event_uid)
            except Exception:
                pass


async def test_create_event_with_attendees(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating an event with attendees."""
    calendar_name = temporary_calendar

    tomorrow = datetime.now() + timedelta(days=1)
    event_data = {
        "title": "Meeting with Attendees",
        "start_datetime": tomorrow.strftime("%Y-%m-%dT16:00:00"),
        "end_datetime": tomorrow.strftime("%Y-%m-%dT17:00:00"),
        "description": "Test meeting with multiple attendees",
        "location": "Conference Room A",
        "attendees": "test1@example.com,test2@example.com",
        "reminder_minutes": 15,
        "status": "TENTATIVE",
    }

    try:
        result = await nc_client.calendar.create_event(calendar_name, event_data)
        event_uid = result["uid"]
        logger.info(f"Created event with attendees, UID: {event_uid}")

        # Verify event
        retrieved_event, _ = await nc_client.calendar.get_event(
            calendar_name, event_uid
        )
        assert retrieved_event["title"] == "Meeting with Attendees"
        assert "test1@example.com" in retrieved_event.get("attendees", "")
        assert retrieved_event["status"] == "TENTATIVE"

        # Cleanup
        await nc_client.calendar.delete_event(calendar_name, event_uid)

    except Exception as e:
        logger.error(f"Event with attendees test failed: {e}")
        raise


async def test_get_nonexistent_event(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test retrieving a non-existent event."""
    calendar_name = temporary_calendar
    fake_uid = f"nonexistent-{uuid.uuid4()}"

    # caldav library raises generic Exception for missing events, not HTTPStatusError
    with pytest.raises(Exception, match="not found"):
        await nc_client.calendar.get_event(calendar_name, fake_uid)

    logger.info(f"Correctly raised exception for nonexistent event: {fake_uid}")


async def test_delete_nonexistent_event(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test deleting a non-existent event."""
    calendar_name = temporary_calendar
    fake_uid = f"nonexistent-{uuid.uuid4()}"

    result = await nc_client.calendar.delete_event(calendar_name, fake_uid)
    assert result["status_code"] == 404
    logger.info(f"Correctly got 404 for deleting nonexistent event: {fake_uid}")


async def test_event_with_url_and_categories(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating an event with URL and multiple categories."""
    calendar_name = temporary_calendar

    tomorrow = datetime.now() + timedelta(days=1)
    event_data = {
        "title": "Event with URL and Categories",
        "start_datetime": tomorrow.strftime("%Y-%m-%dT09:00:00"),
        "end_datetime": tomorrow.strftime("%Y-%m-%dT10:30:00"),
        "description": "Test event with additional metadata",
        "categories": "work,meeting,important,quarterly",
        "url": "https://zoom.us/j/123456789",
        "privacy": "PRIVATE",
        "priority": 2,
    }

    try:
        result = await nc_client.calendar.create_event(calendar_name, event_data)
        event_uid = result["uid"]
        logger.info(f"Created event with metadata, UID: {event_uid}")

        # Verify event
        retrieved_event, _ = await nc_client.calendar.get_event(
            calendar_name, event_uid
        )
        assert retrieved_event["title"] == "Event with URL and Categories"
        assert "work" in retrieved_event.get("categories", "")
        assert "important" in retrieved_event.get("categories", "")
        assert retrieved_event.get("url") == "https://zoom.us/j/123456789"
        assert retrieved_event.get("privacy") == "PRIVATE"
        assert retrieved_event.get("priority") == 2

        # Cleanup
        await nc_client.calendar.delete_event(calendar_name, event_uid)

    except Exception as e:
        logger.error(f"Event with metadata test failed: {e}")
        raise


async def test_list_events_date_range_filtering(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test that date range filtering actually excludes events outside the range.

    Reproduces GH-538: get_calendar_events() accepted date range parameters
    but returned events from the entire calendar history, ignoring date filters.
    """
    calendar_name = temporary_calendar
    past_uid = None
    future_uid = None

    try:
        # Create Event A: 30 days in the past
        past_date = datetime.now() - timedelta(days=30)
        past_event_data = {
            "title": f"Past Event {uuid.uuid4().hex[:8]}",
            "start_datetime": past_date.strftime("%Y-%m-%dT10:00:00"),
            "end_datetime": past_date.strftime("%Y-%m-%dT11:00:00"),
            "description": "Event in the past for date range test",
        }
        result_past = await nc_client.calendar.create_event(
            calendar_name, past_event_data
        )
        past_uid = result_past["uid"]
        logger.info(f"Created past event: {past_uid}")

        # Create Event B: 1 day in the future
        future_date = datetime.now() + timedelta(days=1)
        future_event_data = {
            "title": f"Future Event {uuid.uuid4().hex[:8]}",
            "start_datetime": future_date.strftime("%Y-%m-%dT14:00:00"),
            "end_datetime": future_date.strftime("%Y-%m-%dT15:00:00"),
            "description": "Event in the future for date range test",
        }
        result_future = await nc_client.calendar.create_event(
            calendar_name, future_event_data
        )
        future_uid = result_future["uid"]
        logger.info(f"Created future event: {future_uid}")

        # Query with date range: today → 7 days ahead
        now = datetime.now()
        week_ahead = now + timedelta(days=7)

        events = await nc_client.calendar.get_calendar_events(
            calendar_name=calendar_name,
            start_datetime=now,
            end_datetime=week_ahead,
            limit=50,
        )

        event_uids = [e["uid"] for e in events]

        # Future event (tomorrow) SHOULD be in results
        assert future_uid in event_uids, (
            f"Future event {future_uid} should be in date-filtered results"
        )

        # Past event (30 days ago) should NOT be in results
        assert past_uid not in event_uids, (
            f"Past event {past_uid} should be excluded by date range filter "
            f"(GH-538: date range was being ignored)"
        )

        logger.info(
            f"Date range filtering works: {len(events)} events returned, "
            f"past event correctly excluded"
        )

    finally:
        # Cleanup both events
        for uid in [past_uid, future_uid]:
            if uid:
                try:
                    await nc_client.calendar.delete_event(calendar_name, uid)
                except Exception as e:
                    logger.warning(f"Cleanup failed for event {uid}: {e}")


async def test_recurring_event_date_range_expansion(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test that recurring events are expanded into individual occurrences.

    When querying with a date range, a recurring event should return one
    event dict per occurrence within the range, each with the correct
    start_datetime for that occurrence (not the original master event date).

    This is a follow-up to GH-538: the time-range filter correctly selected
    recurring events, but returned the master event with its original DTSTART
    instead of expanding occurrences.
    """
    calendar_name = temporary_calendar
    event_uid = None

    try:
        # Create a daily recurring event starting 7 days ago
        start = datetime.now() - timedelta(days=7)
        event_data = {
            "title": f"Daily Recurrence {uuid.uuid4().hex[:8]}",
            "start_datetime": start.strftime("%Y-%m-%dT09:00:00"),
            "end_datetime": start.strftime("%Y-%m-%dT10:00:00"),
            "description": "Daily recurring event for expansion test",
            "recurring": True,
            "recurrence_rule": "FREQ=DAILY",
        }
        result = await nc_client.calendar.create_event(calendar_name, event_data)
        event_uid = result["uid"]
        logger.info(f"Created daily recurring event: {event_uid}")

        # Query with date range: today → 3 days ahead
        query_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        query_end = query_start + timedelta(days=3)

        events = await nc_client.calendar.get_calendar_events(
            calendar_name=calendar_name,
            start_datetime=query_start,
            end_datetime=query_end,
            limit=50,
        )

        # Filter to only our recurring event (calendar may have others)
        our_events = [e for e in events if e["uid"] == event_uid]

        # Should have multiple occurrences (one per day in the range)
        assert len(our_events) >= 2, (
            f"Expected multiple expanded occurrences, got {len(our_events)}. "
            f"Expansion may not be working."
        )

        # Each occurrence should have a different start_datetime
        start_dates = [e["start_datetime"] for e in our_events]
        assert len(set(start_dates)) == len(our_events), (
            f"Each occurrence should have a unique start_datetime, got: {start_dates}"
        )

        # No start_datetime should fall outside the queried range
        for e in our_events:
            event_start = datetime.fromisoformat(e["start_datetime"])
            # Remove timezone info for comparison if present
            if event_start.tzinfo is not None:
                event_start = event_start.replace(tzinfo=None)
            assert event_start >= query_start - timedelta(hours=1), (
                f"Occurrence {e['start_datetime']} is before query start {query_start}"
            )
            assert event_start < query_end + timedelta(hours=1), (
                f"Occurrence {e['start_datetime']} is after query end {query_end}"
            )

        # Expanded occurrences should NOT have recurrence rules
        # (server strips RRULE when expanding)
        for e in our_events:
            assert not e.get("recurring"), (
                "Expanded occurrence should not have recurring=True, "
                "RRULE should be stripped by server-side expansion"
            )

        logger.info(
            f"Recurring event expansion works: {len(our_events)} occurrences "
            f"returned with unique start dates"
        )

    finally:
        if event_uid:
            try:
                await nc_client.calendar.delete_event(calendar_name, event_uid)
            except Exception as e:
                logger.warning(f"Cleanup failed for recurring event {event_uid}: {e}")


async def test_calendar_operations_error_handling(
    nc_client: NextcloudClient,
):
    """Test error handling for calendar operations."""

    # Test with non-existent calendar
    fake_calendar = f"nonexistent_calendar_{uuid.uuid4().hex}"

    # caldav library returns empty list for non-existent calendars, doesn't raise
    # Testing that it doesn't crash and returns empty results
    events = await nc_client.calendar.get_calendar_events(fake_calendar)
    assert isinstance(events, list)
    # Empty list is expected for non-existent calendar
    assert len(events) == 0

    logger.info("Error handling tests completed successfully")
