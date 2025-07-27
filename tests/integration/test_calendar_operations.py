"""Integration tests for Calendar CalDAV operations."""

import pytest
import logging
import uuid
from datetime import datetime, timedelta
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture
def test_calendar_name():
    """Unique calendar name for testing."""
    return f"test_calendar_{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def temporary_calendar(nc_client: NextcloudClient, test_calendar_name: str):
    """Create a temporary calendar for testing and clean up afterward."""
    calendar_name = test_calendar_name

    try:
        # Create a test calendar if possible
        # Note: Calendar creation might require admin permissions
        # For now, we'll use an existing calendar or create events in default calendar

        # Try to find an existing calendar to use
        calendars = await nc_client.calendar.list_calendars()
        if calendars:
            calendar_name = calendars[0]["name"]
            logger.info(f"Using existing calendar: {calendar_name}")
            yield calendar_name
        else:
            pytest.skip("No calendars available for testing")

    except Exception as e:
        logger.error(f"Error setting up temporary calendar: {e}")
        pytest.skip(f"Calendar setup failed: {e}")


@pytest.fixture
async def temporary_event(nc_client: NextcloudClient, temporary_calendar: str):
    """Create a temporary event for testing and clean up afterward."""
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
    start_date = datetime.now().strftime("%Y%m%dT000000Z")
    end_date = (datetime.now() + timedelta(days=7)).strftime("%Y%m%dT235959Z")

    events = await nc_client.calendar.get_calendar_events(
        calendar_name=calendar_name, start_date=start_date, end_date=end_date, limit=50
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

    with pytest.raises(HTTPStatusError) as exc_info:
        await nc_client.calendar.get_event(calendar_name, fake_uid)

    assert exc_info.value.response.status_code == 404
    logger.info(f"Correctly got 404 for nonexistent event: {fake_uid}")


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


async def test_calendar_operations_error_handling(nc_client: NextcloudClient):
    """Test error handling for calendar operations."""

    # Test with non-existent calendar
    fake_calendar = f"nonexistent_calendar_{uuid.uuid4().hex}"

    with pytest.raises(HTTPStatusError):
        await nc_client.calendar.get_calendar_events(fake_calendar)

    logger.info("Error handling tests completed successfully")
