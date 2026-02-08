"""Integration tests for Calendar VEVENT update MCP tools - extended fields."""

import json
import logging
from datetime import datetime, timedelta

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


async def test_mcp_update_event_extended_fields(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """Test updating categories, recurrence_rule, attendees, and reminder_minutes via MCP."""

    calendar_name = temporary_calendar
    event_uid = None

    try:
        # 1. Create a base event via MCP
        tomorrow = datetime.now() + timedelta(days=1)
        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_event",
            {
                "calendar_name": calendar_name,
                "title": "Extended Fields MCP Test",
                "start_datetime": tomorrow.strftime("%Y-%m-%dT14:00:00"),
                "end_datetime": tomorrow.strftime("%Y-%m-%dT15:00:00"),
                "description": "Base event for MCP extended-field update test",
            },
        )
        assert create_result.isError is False, (
            f"MCP event creation failed: {create_result.content}"
        )

        result_data = json.loads(create_result.content[0].text)
        event_uid = result_data["uid"]
        logger.info(f"Created base event via MCP: {event_uid}")

        # 2. Update with all four extended fields via MCP
        update_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_event",
            {
                "calendar_name": calendar_name,
                "event_uid": event_uid,
                "categories": "work,meeting",
                "recurrence_rule": "FREQ=WEEKLY;COUNT=4",
                "attendees": "alice@example.com,bob@example.com",
                "reminder_minutes": 15,
            },
        )
        assert update_result.isError is False, (
            f"MCP event update failed: {update_result.content}"
        )

        # 3. Verify via direct client
        event, _ = await nc_client.calendar.get_event(calendar_name, event_uid)

        # Categories
        assert "work" in event.get("categories", ""), (
            f"Expected 'work' in categories, got: {event.get('categories')}"
        )
        assert "meeting" in event.get("categories", ""), (
            f"Expected 'meeting' in categories, got: {event.get('categories')}"
        )

        # Recurrence
        assert event.get("recurring") is True, "Expected event to be recurring"
        assert "WEEKLY" in event.get("recurrence_rule", ""), (
            f"Expected WEEKLY in rrule, got: {event.get('recurrence_rule')}"
        )

        # Attendees
        attendees = event.get("attendees", "")
        assert "alice@example.com" in attendees, (
            f"Expected alice in attendees, got: {attendees}"
        )
        assert "bob@example.com" in attendees, (
            f"Expected bob in attendees, got: {attendees}"
        )

        logger.info("MCP extended fields update verified successfully")

        # 4. Clear all four fields via MCP
        clear_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_event",
            {
                "calendar_name": calendar_name,
                "event_uid": event_uid,
                "categories": "",
                "recurrence_rule": "",
                "attendees": "",
                "reminder_minutes": 0,
            },
        )
        assert clear_result.isError is False, (
            f"MCP event clear failed: {clear_result.content}"
        )

        # 5. Verify fields cleared
        cleared, _ = await nc_client.calendar.get_event(calendar_name, event_uid)
        assert not cleared.get("categories"), (
            f"Expected categories cleared, got: {cleared.get('categories')}"
        )
        assert cleared.get("recurring") is not True, (
            f"Expected recurring cleared, got: {cleared.get('recurring')}"
        )
        assert not cleared.get("attendees"), (
            f"Expected attendees cleared, got: {cleared.get('attendees')}"
        )

        logger.info("MCP extended fields clear verified successfully")

    finally:
        if event_uid:
            try:
                await nc_client.calendar.delete_event(calendar_name, event_uid)
            except Exception:
                pass
