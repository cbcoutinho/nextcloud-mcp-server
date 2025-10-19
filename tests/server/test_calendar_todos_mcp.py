"""Integration tests for Calendar VTODO (task) MCP tools."""

import logging
from datetime import datetime, timedelta

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


async def test_mcp_todo_complete_workflow(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """Test complete todo workflow via MCP tools with verification via NextcloudClient."""

    calendar_name = temporary_calendar
    todo_uid = None

    try:
        # 1. Create todo via MCP
        logger.info(f"Creating todo in {calendar_name} via MCP")
        tomorrow = datetime.now() + timedelta(days=1)

        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_todo",
            {
                "calendar_name": calendar_name,
                "summary": "MCP Test Task",
                "description": "Test task created via MCP tools",
                "status": "NEEDS-ACTION",
                "priority": 3,
                "due": tomorrow.strftime("%Y-%m-%dT18:00:00"),
                "categories": "testing,mcp",
            },
        )
        assert create_result.isError is False

        # Extract UID from the result
        result_data = create_result.content[0].text
        import json

        result_json = json.loads(result_data)
        todo_uid = result_json["uid"]
        logger.info(f"Created todo with UID: {todo_uid}")

        # 2. Verify todo creation via client
        todos = await nc_client.calendar.list_todos(calendar_name)
        assert any(t["uid"] == todo_uid for t in todos)
        created_todo = next(t for t in todos if t["uid"] == todo_uid)
        assert created_todo["summary"] == "MCP Test Task"
        assert created_todo["status"] == "NEEDS-ACTION"
        assert created_todo["priority"] == 3

        # 3. List todos via MCP
        logger.info(f"Listing todos in {calendar_name} via MCP")
        list_result = await nc_mcp_client.call_tool(
            "nc_calendar_list_todos",
            {"calendar_name": calendar_name},
        )
        assert list_result.isError is False

        list_data = json.loads(list_result.content[0].text)
        assert "todos" in list_data
        assert any(t["uid"] == todo_uid for t in list_data["todos"])

        # 4. Update todo via MCP
        logger.info(f"Updating todo {todo_uid} via MCP")
        update_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_todo",
            {
                "calendar_name": calendar_name,
                "todo_uid": todo_uid,
                "summary": "MCP Test Task Updated",
                "status": "IN-PROCESS",
                "priority": 1,
                "percent_complete": 50,
            },
        )
        assert update_result.isError is False

        # 5. Verify update via client
        todos = await nc_client.calendar.list_todos(calendar_name)
        updated_todo = next(t for t in todos if t["uid"] == todo_uid)
        assert updated_todo["summary"] == "MCP Test Task Updated"
        assert updated_todo["status"] == "IN-PROCESS"
        assert updated_todo["priority"] == 1
        assert updated_todo["percent_complete"] == 50

        # 6. Delete todo via MCP
        logger.info(f"Deleting todo {todo_uid} via MCP")
        delete_result = await nc_mcp_client.call_tool(
            "nc_calendar_delete_todo",
            {"calendar_name": calendar_name, "todo_uid": todo_uid},
        )
        assert delete_result.isError is False

        # 7. Verify deletion via client
        todos = await nc_client.calendar.list_todos(calendar_name)
        assert not any(t["uid"] == todo_uid for t in todos)

        logger.info("Complete todo workflow test passed")

    finally:
        # Cleanup in case of failure
        if todo_uid:
            try:
                await nc_client.calendar.delete_todo(calendar_name, todo_uid)
            except Exception:
                pass


async def test_mcp_list_todos_with_filters(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """Test listing todos with various filters via MCP tools."""

    calendar_name = temporary_calendar
    created_uids = []

    try:
        # Create test todos with different properties
        test_todos = [
            {
                "summary": "High Priority Task",
                "status": "NEEDS-ACTION",
                "priority": 1,
                "categories": "urgent,work",
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

        # Create todos via client
        for todo_data in test_todos:
            result = await nc_client.calendar.create_todo(calendar_name, todo_data)
            created_uids.append(result["uid"])

        # Test 1: Filter by status
        logger.info("Testing filter by status")
        result = await nc_mcp_client.call_tool(
            "nc_calendar_list_todos",
            {"calendar_name": calendar_name, "status": "NEEDS-ACTION"},
        )
        assert result.isError is False
        import json

        data = json.loads(result.content[0].text)
        needs_action_todos = [t for t in data["todos"] if t["uid"] in created_uids]
        assert len(needs_action_todos) == 2  # Two NEEDS-ACTION todos

        # Test 2: Filter by priority
        logger.info("Testing filter by minimum priority")
        result = await nc_mcp_client.call_tool(
            "nc_calendar_list_todos",
            {"calendar_name": calendar_name, "min_priority": 1},
        )
        assert result.isError is False
        data = json.loads(result.content[0].text)
        high_priority_todos = [t for t in data["todos"] if t["uid"] in created_uids]
        assert len(high_priority_todos) >= 1  # At least the priority 1 todo

        # Test 3: Filter by categories
        logger.info("Testing filter by categories")
        result = await nc_mcp_client.call_tool(
            "nc_calendar_list_todos",
            {"calendar_name": calendar_name, "categories": "work"},
        )
        assert result.isError is False
        data = json.loads(result.content[0].text)
        work_todos = [t for t in data["todos"] if t["uid"] in created_uids]
        assert len(work_todos) >= 2  # Two todos with "work" category

        # Test 4: Filter by summary text
        logger.info("Testing filter by summary text")
        result = await nc_mcp_client.call_tool(
            "nc_calendar_list_todos",
            {"calendar_name": calendar_name, "summary_contains": "Priority"},
        )
        assert result.isError is False
        data = json.loads(result.content[0].text)
        priority_todos = [t for t in data["todos"] if t["uid"] in created_uids]
        assert len(priority_todos) == 2  # Two have "Priority" in summary (High, Low)

        logger.info("List todos with filters test passed")

    finally:
        # Cleanup
        for uid in created_uids:
            try:
                await nc_client.calendar.delete_todo(calendar_name, uid)
            except Exception:
                pass


async def test_mcp_search_todos_across_calendars(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_calendar: str,
    shared_calendar_2: str,
):
    """Test searching todos across multiple calendars via MCP tools.

    Note: Uses two shared test calendars to avoid rate limiting.
    """

    cal1_name = temporary_calendar  # First shared test calendar
    cal2_name = shared_calendar_2  # Second shared test calendar
    created_uids = []

    try:
        # Use existing shared calendars (no creation needed, avoiding rate limits)

        # Create todos in both calendars
        result1 = await nc_client.calendar.create_todo(
            cal1_name,
            {
                "summary": "Task in Calendar 1",
                "status": "NEEDS-ACTION",
                "categories": "cal1",
            },
        )
        created_uids.append((cal1_name, result1["uid"]))

        result2 = await nc_client.calendar.create_todo(
            cal2_name,
            {
                "summary": "Task in Calendar 2",
                "status": "IN-PROCESS",
                "categories": "cal2",
            },
        )
        created_uids.append((cal2_name, result2["uid"]))

        # Search across all calendars via MCP
        logger.info("Searching todos across all calendars via MCP")
        search_result = await nc_mcp_client.call_tool(
            "nc_calendar_search_todos",
            {},
        )
        assert search_result.isError is False

        import json

        data = json.loads(search_result.content[0].text)
        assert "todos" in data

        # Verify both todos are in the results
        found_uids = {t["uid"] for t in data["todos"]}
        assert result1["uid"] in found_uids
        assert result2["uid"] in found_uids

        # Verify calendar_name is included
        our_todos = [
            t for t in data["todos"] if t["uid"] in [result1["uid"], result2["uid"]]
        ]
        for todo in our_todos:
            assert "calendar_name" in todo
            assert todo["calendar_name"] in [cal1_name, cal2_name]

        # Test search with status filter
        logger.info("Searching with status filter via MCP")
        search_result = await nc_mcp_client.call_tool(
            "nc_calendar_search_todos",
            {"status": "IN-PROCESS"},
        )
        assert search_result.isError is False
        data = json.loads(search_result.content[0].text)
        in_process_todos = [
            t for t in data["todos"] if t["uid"] in [uid for _, uid in created_uids]
        ]
        assert len(in_process_todos) >= 1

        logger.info("Search todos across calendars test passed")

    finally:
        # Cleanup: Only delete todos, not calendars (they're reused/built-in)
        for cal_name, uid in created_uids:
            try:
                await nc_client.calendar.delete_todo(cal_name, uid)
            except Exception:
                pass


async def test_mcp_todo_status_transitions(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """Test transitioning through different todo statuses via MCP tools."""

    calendar_name = temporary_calendar
    todo_uid = None

    try:
        # Create todo
        result = await nc_client.calendar.create_todo(
            calendar_name,
            {"summary": "Status Transition Test", "status": "NEEDS-ACTION"},
        )
        todo_uid = result["uid"]

        # Transition: NEEDS-ACTION → IN-PROCESS
        logger.info("Transitioning todo to IN-PROCESS via MCP")
        update_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_todo",
            {
                "calendar_name": calendar_name,
                "todo_uid": todo_uid,
                "status": "IN-PROCESS",
                "percent_complete": 25,
            },
        )
        assert update_result.isError is False

        todos = await nc_client.calendar.list_todos(calendar_name)
        todo = next(t for t in todos if t["uid"] == todo_uid)
        assert todo["status"] == "IN-PROCESS"
        assert todo["percent_complete"] == 25

        # Transition: IN-PROCESS → COMPLETED
        logger.info("Transitioning todo to COMPLETED via MCP")
        completed_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        update_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_todo",
            {
                "calendar_name": calendar_name,
                "todo_uid": todo_uid,
                "status": "COMPLETED",
                "percent_complete": 100,
                "completed": completed_time,
            },
        )
        assert update_result.isError is False

        todos = await nc_client.calendar.list_todos(calendar_name)
        todo = next(t for t in todos if t["uid"] == todo_uid)
        assert todo["status"] == "COMPLETED"
        assert todo["percent_complete"] == 100
        assert "completed" in todo

        logger.info("Todo status transitions test passed")

    finally:
        if todo_uid:
            try:
                await nc_client.calendar.delete_todo(calendar_name, todo_uid)
            except Exception:
                pass


async def test_mcp_todo_with_dates(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating and managing todos with date fields via MCP tools."""

    calendar_name = temporary_calendar
    todo_uid = None

    try:
        now = datetime.now()
        start_date = (now + timedelta(days=1)).strftime("%Y-%m-%dT09:00:00")
        due_date = (now + timedelta(days=7)).strftime("%Y-%m-%dT17:00:00")

        # Create todo with dates via MCP
        logger.info("Creating todo with dates via MCP")
        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_todo",
            {
                "calendar_name": calendar_name,
                "summary": "Task with Dates",
                "description": "Test task with various date fields",
                "status": "NEEDS-ACTION",
                "dtstart": start_date,
                "due": due_date,
            },
        )
        assert create_result.isError is False

        import json

        result_data = json.loads(create_result.content[0].text)
        todo_uid = result_data["uid"]

        # Verify dates via client
        todos = await nc_client.calendar.list_todos(calendar_name)
        created_todo = next(t for t in todos if t["uid"] == todo_uid)
        assert created_todo["summary"] == "Task with Dates"
        assert "dtstart" in created_todo
        assert "due" in created_todo

        logger.info("Todo with dates test passed")

    finally:
        if todo_uid:
            try:
                await nc_client.calendar.delete_todo(calendar_name, todo_uid)
            except Exception:
                pass


async def test_mcp_todo_categories(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """Test creating and managing todos with categories via MCP tools."""

    calendar_name = temporary_calendar
    todo_uid = None

    try:
        # Create todo with multiple categories via MCP
        logger.info("Creating todo with categories via MCP")
        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_todo",
            {
                "calendar_name": calendar_name,
                "summary": "Task with Categories",
                "status": "NEEDS-ACTION",
                "categories": "work,meeting,important,quarterly",
            },
        )
        assert create_result.isError is False

        import json

        result_data = json.loads(create_result.content[0].text)
        todo_uid = result_data["uid"]

        # Verify categories via client
        todos = await nc_client.calendar.list_todos(calendar_name)
        created_todo = next(t for t in todos if t["uid"] == todo_uid)
        assert "categories" in created_todo
        categories_str = created_todo["categories"]
        assert "work" in categories_str
        assert "meeting" in categories_str
        assert "important" in categories_str
        assert "quarterly" in categories_str

        # Update categories via MCP
        logger.info("Updating todo categories via MCP")
        update_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_todo",
            {
                "calendar_name": calendar_name,
                "todo_uid": todo_uid,
                "categories": "updated,new-category",
            },
        )
        assert update_result.isError is False

        # Verify updated categories
        todos = await nc_client.calendar.list_todos(calendar_name)
        updated_todo = next(t for t in todos if t["uid"] == todo_uid)
        categories_str = updated_todo["categories"]
        assert "updated" in categories_str
        assert "new-category" in categories_str

        logger.info("Todo categories test passed")

    finally:
        if todo_uid:
            try:
                await nc_client.calendar.delete_todo(calendar_name, todo_uid)
            except Exception:
                pass
