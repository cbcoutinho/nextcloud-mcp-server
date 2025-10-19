"""Integration tests for Calendar VTODO (task) operations."""

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
async def temporary_todo(nc_client: NextcloudClient, temporary_calendar: str):
    """Create a temporary todo for testing and clean up afterward."""
    todo_uid = None
    calendar_name = temporary_calendar

    # Create a test todo
    tomorrow = datetime.now() + timedelta(days=1)
    todo_data = {
        "summary": f"Test Task {uuid.uuid4().hex[:8]}",
        "description": "Test todo created by integration tests",
        "status": "NEEDS-ACTION",
        "priority": 5,
        "due": tomorrow.strftime("%Y-%m-%dT18:00:00"),
        "categories": "testing",
    }

    try:
        logger.info(f"Creating temporary todo in calendar: {calendar_name}")
        result = await nc_client.calendar.create_todo(calendar_name, todo_data)
        todo_uid = result.get("uid")

        if not todo_uid:
            pytest.fail("Failed to create temporary todo")

        logger.info(f"Created temporary todo with UID: {todo_uid}")
        yield {"uid": todo_uid, "calendar_name": calendar_name, "data": todo_data}

    finally:
        # Cleanup
        if todo_uid:
            try:
                logger.info(f"Cleaning up temporary todo: {todo_uid}")
                await nc_client.calendar.delete_todo(calendar_name, todo_uid)
                logger.info(f"Successfully deleted temporary todo: {todo_uid}")
            except HTTPStatusError as e:
                if e.response.status_code != 404:
                    logger.error(f"Error deleting temporary todo {todo_uid}: {e}")
            except Exception as e:
                logger.error(
                    f"Unexpected error deleting temporary todo {todo_uid}: {e}"
                )


# ============= Basic CRUD Tests =============


async def test_create_and_delete_todo(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating and deleting a basic todo."""
    calendar_name = temporary_calendar

    # Create todo
    tomorrow = datetime.now() + timedelta(days=1)
    todo_data = {
        "summary": "Integration Test Task",
        "description": "Test task for integration testing",
        "status": "NEEDS-ACTION",
        "priority": 3,
        "due": tomorrow.strftime("%Y-%m-%dT18:00:00"),
        "categories": "testing,integration",
    }

    try:
        result = await nc_client.calendar.create_todo(calendar_name, todo_data)
        assert "uid" in result
        assert result["status_code"] in [200, 201, 204]

        todo_uid = result["uid"]
        logger.info(f"Created todo with UID: {todo_uid}")

        # Verify todo was created by listing todos
        todos = await nc_client.calendar.list_todos(calendar_name)
        todo_uids = [todo.get("uid") for todo in todos]
        assert todo_uid in todo_uids

        # Find our todo in the list
        our_todo = next((t for t in todos if t.get("uid") == todo_uid), None)
        assert our_todo is not None
        assert our_todo["summary"] == "Integration Test Task"
        assert our_todo["status"] == "NEEDS-ACTION"
        assert our_todo["priority"] == 3

        # Delete todo
        delete_result = await nc_client.calendar.delete_todo(calendar_name, todo_uid)
        assert delete_result["status_code"] in [200, 204, 404]

        logger.info(f"Successfully deleted todo: {todo_uid}")

    except Exception as e:
        logger.error(f"Test failed: {e}")
        raise


async def test_list_todos(nc_client: NextcloudClient, temporary_calendar: str):
    """Test listing todos in a calendar."""
    calendar_name = temporary_calendar

    # Create multiple todos
    todo_uids = []
    for i in range(3):
        todo_data = {
            "summary": f"Test Task {i + 1}",
            "description": f"Task number {i + 1}",
            "status": "NEEDS-ACTION",
            "priority": i + 1,
        }
        result = await nc_client.calendar.create_todo(calendar_name, todo_data)
        todo_uids.append(result["uid"])

    try:
        # List todos
        todos = await nc_client.calendar.list_todos(calendar_name)

        assert isinstance(todos, list)
        assert len(todos) >= 3  # At least our 3 todos

        # Check structure
        for todo in todos:
            assert "uid" in todo
            assert "summary" in todo
            assert "status" in todo
            assert "priority" in todo

        # Verify our todos are in the list
        listed_uids = [todo["uid"] for todo in todos]
        for uid in todo_uids:
            assert uid in listed_uids

        logger.info(f"Found {len(todos)} todos in calendar")

    finally:
        # Cleanup
        for uid in todo_uids:
            try:
                await nc_client.calendar.delete_todo(calendar_name, uid)
            except Exception:
                pass


async def test_update_todo(nc_client: NextcloudClient, temporary_todo: dict):
    """Test updating an existing todo."""
    calendar_name = temporary_todo["calendar_name"]
    todo_uid = temporary_todo["uid"]

    # Update todo data
    updated_data = {
        "summary": "Updated Test Task Title",
        "description": "Updated description for test task",
        "status": "IN-PROCESS",
        "priority": 1,  # High priority
        "percent_complete": 50,
    }

    try:
        result = await nc_client.calendar.update_todo(
            calendar_name, todo_uid, updated_data
        )
        assert result["uid"] == todo_uid

        # Verify updates by listing todos
        todos = await nc_client.calendar.list_todos(calendar_name)
        updated_todo = next((t for t in todos if t["uid"] == todo_uid), None)

        assert updated_todo is not None
        assert updated_todo["summary"] == "Updated Test Task Title"
        assert updated_todo["description"] == "Updated description for test task"
        assert updated_todo["status"] == "IN-PROCESS"
        assert updated_todo["priority"] == 1
        assert updated_todo["percent_complete"] == 50

        logger.info(f"Successfully updated todo: {todo_uid}")

    except Exception as e:
        logger.error(f"Todo update test failed: {e}")
        raise


async def test_todo_with_dates(nc_client: NextcloudClient, temporary_calendar: str):
    """Test creating a todo with start, due, and completed dates."""
    calendar_name = temporary_calendar

    now = datetime.now()
    start_date = now + timedelta(days=1)
    due_date = now + timedelta(days=7)

    todo_data = {
        "summary": "Task with Dates",
        "description": "Test task with various date fields",
        "status": "NEEDS-ACTION",
        "dtstart": start_date.strftime("%Y-%m-%dT09:00:00"),
        "due": due_date.strftime("%Y-%m-%dT17:00:00"),
    }

    try:
        result = await nc_client.calendar.create_todo(calendar_name, todo_data)
        todo_uid = result["uid"]
        logger.info(f"Created todo with dates, UID: {todo_uid}")

        # Verify dates
        todos = await nc_client.calendar.list_todos(calendar_name)
        created_todo = next((t for t in todos if t["uid"] == todo_uid), None)

        assert created_todo is not None
        assert created_todo["summary"] == "Task with Dates"
        assert "dtstart" in created_todo
        assert "due" in created_todo

        # Cleanup
        await nc_client.calendar.delete_todo(calendar_name, todo_uid)

    except Exception as e:
        logger.error(f"Date handling test failed: {e}")
        raise


# ============= Advanced Feature Tests =============


async def test_todo_status_transitions(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test transitioning through different todo statuses."""
    calendar_name = temporary_calendar

    todo_data = {
        "summary": "Status Transition Test",
        "description": "Testing status changes",
        "status": "NEEDS-ACTION",
    }

    result = await nc_client.calendar.create_todo(calendar_name, todo_data)
    todo_uid = result["uid"]

    try:
        # Transition: NEEDS-ACTION → IN-PROCESS
        await nc_client.calendar.update_todo(
            calendar_name,
            todo_uid,
            {"status": "IN-PROCESS", "percent_complete": 25},
        )

        todos = await nc_client.calendar.list_todos(calendar_name)
        todo = next((t for t in todos if t["uid"] == todo_uid), None)
        assert todo["status"] == "IN-PROCESS"
        assert todo["percent_complete"] == 25

        # Transition: IN-PROCESS → COMPLETED
        completed_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        await nc_client.calendar.update_todo(
            calendar_name,
            todo_uid,
            {
                "status": "COMPLETED",
                "percent_complete": 100,
                "completed": completed_time,
            },
        )

        todos = await nc_client.calendar.list_todos(calendar_name)
        todo = next((t for t in todos if t["uid"] == todo_uid), None)
        assert todo["status"] == "COMPLETED"
        assert todo["percent_complete"] == 100
        assert "completed" in todo

        logger.info(f"Successfully transitioned todo through statuses: {todo_uid}")

    finally:
        await nc_client.calendar.delete_todo(calendar_name, todo_uid)


async def test_todo_priority_levels(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test different priority levels (0=undefined, 1=highest, 9=lowest)."""
    calendar_name = temporary_calendar
    priorities = [0, 1, 5, 9]
    priority_labels = {0: "Undefined", 1: "Highest", 5: "Medium", 9: "Lowest"}
    todo_uids = []

    try:
        # Create todos with different priorities
        for priority in priorities:
            todo_data = {
                "summary": f"Priority {priority} Task ({priority_labels[priority]})",
                "status": "NEEDS-ACTION",
                "priority": priority,
            }
            result = await nc_client.calendar.create_todo(calendar_name, todo_data)
            todo_uids.append((result["uid"], priority))

        # Verify all priorities
        todos = await nc_client.calendar.list_todos(calendar_name)

        for uid, expected_priority in todo_uids:
            todo = next((t for t in todos if t["uid"] == uid), None)
            assert todo is not None
            assert todo["priority"] == expected_priority

        logger.info(f"Successfully tested priority levels: {priorities}")

    finally:
        # Cleanup
        for uid, _ in todo_uids:
            try:
                await nc_client.calendar.delete_todo(calendar_name, uid)
            except Exception:
                pass


async def test_todo_with_categories(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating a todo with multiple categories."""
    calendar_name = temporary_calendar

    todo_data = {
        "summary": "Task with Categories",
        "description": "Testing category support",
        "status": "NEEDS-ACTION",
        "categories": "work,meeting,important,quarterly",
    }

    try:
        result = await nc_client.calendar.create_todo(calendar_name, todo_data)
        todo_uid = result["uid"]
        logger.info(f"Created todo with categories, UID: {todo_uid}")

        # Verify categories
        todos = await nc_client.calendar.list_todos(calendar_name)
        created_todo = next((t for t in todos if t["uid"] == todo_uid), None)

        assert created_todo is not None
        assert "categories" in created_todo
        categories_str = created_todo["categories"]
        assert "work" in categories_str
        assert "meeting" in categories_str
        assert "important" in categories_str
        assert "quarterly" in categories_str

        # Cleanup
        await nc_client.calendar.delete_todo(calendar_name, todo_uid)

    except Exception as e:
        logger.error(f"Categories test failed: {e}")
        raise


async def test_search_todos_across_calendars(
    nc_client: NextcloudClient, temporary_calendar: str, shared_calendar_2: str
):
    """Test searching for todos across multiple calendars.

    Uses two shared test calendars to avoid rate limiting.
    """
    # Use existing shared calendars to avoid rate limits
    cal1_name = temporary_calendar  # First shared test calendar
    cal2_name = shared_calendar_2  # Second shared test calendar

    try:
        # Create todos in both calendars
        todo1_data = {"summary": "Task in Calendar 1", "status": "NEEDS-ACTION"}
        todo2_data = {"summary": "Task in Calendar 2", "status": "IN-PROCESS"}

        result1 = await nc_client.calendar.create_todo(cal1_name, todo1_data)
        result2 = await nc_client.calendar.create_todo(cal2_name, todo2_data)

        # Search across all calendars
        all_todos = await nc_client.calendar.search_todos_across_calendars()

        assert isinstance(all_todos, list)

        # Find our todos
        todo1 = next((t for t in all_todos if t["uid"] == result1["uid"]), None)
        todo2 = next((t for t in all_todos if t["uid"] == result2["uid"]), None)

        assert todo1 is not None
        assert todo2 is not None
        assert "calendar_name" in todo1
        assert "calendar_name" in todo2
        assert todo1["calendar_name"] == cal1_name
        assert todo2["calendar_name"] == cal2_name

        logger.info(f"Found {len(all_todos)} todos across all calendars")

    finally:
        # Cleanup: Delete only the todos we created (calendars are reused/built-in)
        try:
            await nc_client.calendar.delete_todo(cal1_name, result1["uid"])
        except Exception:
            pass
        try:
            await nc_client.calendar.delete_todo(cal2_name, result2["uid"])
        except Exception:
            pass


# ============= Edge Case Tests =============


async def test_get_nonexistent_todo(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test attempting to retrieve a non-existent todo."""
    calendar_name = temporary_calendar
    fake_uid = f"nonexistent-{uuid.uuid4()}"

    # List todos to ensure it doesn't exist
    todos = await nc_client.calendar.list_todos(calendar_name)
    matching_todos = [t for t in todos if t.get("uid") == fake_uid]
    assert len(matching_todos) == 0

    logger.info(f"Verified nonexistent todo UID: {fake_uid}")


async def test_delete_nonexistent_todo(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test deleting a non-existent todo."""
    calendar_name = temporary_calendar
    fake_uid = f"nonexistent-{uuid.uuid4()}"

    result = await nc_client.calendar.delete_todo(calendar_name, fake_uid)
    assert result["status_code"] == 404
    logger.info(f"Correctly got 404 for deleting nonexistent todo: {fake_uid}")


async def test_list_todos_with_filters(
    nc_client: NextcloudClient, temporary_calendar: str
):
    """Test listing todos with various filters."""
    calendar_name = temporary_calendar

    # Create todos with different statuses and priorities
    test_todos = [
        {
            "summary": "High Priority Task",
            "status": "NEEDS-ACTION",
            "priority": 1,
            "categories": "urgent",
        },
        {
            "summary": "In Progress Task",
            "status": "IN-PROCESS",
            "priority": 5,
            "categories": "work",
        },
        {
            "summary": "Low Priority Task",
            "status": "NEEDS-ACTION",
            "priority": 9,
            "categories": "someday",
        },
    ]

    created_uids = []

    try:
        # Create test todos
        for todo_data in test_todos:
            result = await nc_client.calendar.create_todo(calendar_name, todo_data)
            created_uids.append(result["uid"])

        # Test basic list without filters
        all_todos = await nc_client.calendar.list_todos(calendar_name)
        assert len(all_todos) >= 3

        # Verify all our todos are in the list
        our_todo_uids = [t["uid"] for t in all_todos if t["uid"] in created_uids]
        assert len(our_todo_uids) == 3

        logger.info(f"Successfully created and listed {len(created_uids)} test todos")

    finally:
        # Cleanup
        for uid in created_uids:
            try:
                await nc_client.calendar.delete_todo(calendar_name, uid)
            except Exception:
                pass
