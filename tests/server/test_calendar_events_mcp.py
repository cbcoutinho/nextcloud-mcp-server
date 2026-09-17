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
        assert create_result.is_error is False, (
            f"MCP event creation failed: {create_result.content}"
        )

        result_data = json.loads(create_result.content[0].text)
        event_uid = result_data["uid"]
        logger.info("Created base event via MCP: %s", event_uid)

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
        assert update_result.is_error is False, (
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
        assert clear_result.is_error is False, (
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


async def test_mcp_create_event_writes_previously_ignored_fields(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """recurrence_end_date, color and reminder_email reach the stored iCal (GH #1251).

    All three were accepted and documented by the tool but consumed by nothing,
    so the round trip through a real Nextcloud is the assertion that matters —
    a unit test on the serializer would have passed before the fix as well.
    """
    calendar_name = temporary_calendar
    event_uid = None

    try:
        tomorrow = datetime.now() + timedelta(days=1)
        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_event",
            {
                "calendar_name": calendar_name,
                "title": "Weekly sync",
                "start_datetime": tomorrow.strftime("%Y-%m-%dT14:00:00"),
                "end_datetime": tomorrow.strftime("%Y-%m-%dT15:00:00"),
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
                "recurrence_end_date": "2026-12-31",
                "color": "tomato",
                "reminder_minutes": 15,
                "reminder_email": True,
            },
        )
        assert create_result.is_error is False, (
            f"MCP event creation failed: {create_result.content}"
        )
        event_uid = json.loads(create_result.content[0].text)["uid"]

        event, _ = await nc_client.calendar.get_event(calendar_name, event_uid)

        assert "UNTIL=20261231" in event.get("recurrence_rule", ""), (
            f"Expected UNTIL in rrule, got: {event.get('recurrence_rule')}"
        )
        assert event.get("color") == "tomato", (
            f"Expected COLOR persisted, got: {event.get('color')}"
        )
        actions = [r["action"] for r in event.get("reminders", [])]
        assert actions == ["DISPLAY", "EMAIL"], (
            f"Expected a DISPLAY and an EMAIL alarm, got: {actions}"
        )

        logger.info("MCP create with previously-ignored fields verified")

    finally:
        if event_uid:
            try:
                await nc_client.calendar.delete_event(calendar_name, event_uid)
            except Exception:
                pass


async def test_mcp_update_event_shorthand_reminder_fields_are_independent(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """Updating one shorthand reminder field must not erase the other's effect.

    Against a real store rather than a synthesised iCal, because the bug this
    guards was precisely that the update path rebuilt alarms from the request
    alone and never consulted what was already saved.
    """
    calendar_name = temporary_calendar
    event_uid = None

    try:
        tomorrow = datetime.now() + timedelta(days=1)
        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_event",
            {
                "calendar_name": calendar_name,
                "title": "Retro",
                "start_datetime": tomorrow.strftime("%Y-%m-%dT14:00:00"),
                "end_datetime": tomorrow.strftime("%Y-%m-%dT15:00:00"),
                "reminder_minutes": 15,
                "reminder_email": True,
            },
        )
        assert create_result.is_error is False, (
            f"MCP event creation failed: {create_result.content}"
        )
        event_uid = json.loads(create_result.content[0].text)["uid"]

        # Change only the offset: the EMAIL alarm must survive at the new offset.
        update_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_event",
            {
                "calendar_name": calendar_name,
                "event_uid": event_uid,
                "reminder_minutes": 45,
            },
        )
        assert update_result.is_error is False, (
            f"MCP event update failed: {update_result.content}"
        )

        event, _ = await nc_client.calendar.get_event(calendar_name, event_uid)
        reminders = event.get("reminders", [])
        assert sorted(r["action"] for r in reminders) == ["DISPLAY", "EMAIL"], (
            f"Expected both alarms preserved, got: {reminders}"
        )
        assert {r.get("minutes_before") for r in reminders} == {45}, (
            f"Expected both alarms moved to 45 minutes, got: {reminders}"
        )

        logger.info("MCP shorthand reminder independence verified")

    finally:
        if event_uid:
            try:
                await nc_client.calendar.delete_event(calendar_name, event_uid)
            except Exception:
                pass


async def test_mcp_update_event_ordered_reminders(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """Ordered reminders survive a real CalDAV round trip (supersedes PR #969).

    Order, an absolute trigger and the preserve-vs-clear distinction are the
    parts a server can quietly mangle, so all three are checked after storage
    rather than at serialization time.
    """
    calendar_name = temporary_calendar
    event_uid = None

    try:
        tomorrow = datetime.now() + timedelta(days=1)
        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_event",
            {
                "calendar_name": calendar_name,
                "title": "Launch review",
                "start_datetime": tomorrow.strftime("%Y-%m-%dT14:00:00"),
                "end_datetime": tomorrow.strftime("%Y-%m-%dT15:00:00"),
                "reminders": [
                    {"action": "EMAIL", "minutes_before": 1440, "description": "Prep"},
                    {"action": "DISPLAY", "minutes_before": 10, "description": "Now"},
                ],
            },
        )
        assert create_result.is_error is False, (
            f"MCP event creation failed: {create_result.content}"
        )
        event_uid = json.loads(create_result.content[0].text)["uid"]

        event, _ = await nc_client.calendar.get_event(calendar_name, event_uid)
        stored = event.get("reminders", [])
        assert [r["description"] for r in stored] == ["Prep", "Now"], (
            f"Expected reminder order preserved, got: {stored}"
        )
        assert [r["action"] for r in stored] == ["EMAIL", "DISPLAY"]
        assert [r["minutes_before"] for r in stored] == [1440, 10]

        # An unrelated update must leave the alarms alone.
        touch_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_event",
            {
                "calendar_name": calendar_name,
                "event_uid": event_uid,
                "location": "Room 2",
            },
        )
        assert touch_result.is_error is False, (
            f"MCP event update failed: {touch_result.content}"
        )
        touched, _ = await nc_client.calendar.get_event(calendar_name, event_uid)
        assert len(touched.get("reminders", [])) == 2, (
            f"Expected reminders preserved, got: {touched.get('reminders')}"
        )

        # An explicit empty list clears them.
        clear_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_event",
            {
                "calendar_name": calendar_name,
                "event_uid": event_uid,
                "reminders": [],
            },
        )
        assert clear_result.is_error is False, (
            f"MCP reminder clear failed: {clear_result.content}"
        )
        cleared, _ = await nc_client.calendar.get_event(calendar_name, event_uid)
        assert not cleared.get("reminders"), (
            f"Expected reminders cleared, got: {cleared.get('reminders')}"
        )

        logger.info("MCP ordered reminders verified")

    finally:
        if event_uid:
            try:
                await nc_client.calendar.delete_event(calendar_name, event_uid)
            except Exception:
                pass


async def test_mcp_create_event_with_attendees_sets_organizer(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """GH #1497: without ORGANIZER, Nextcloud sends attendees no CANCEL on delete."""
    user = nc_client.username
    original_email = (await nc_client.users.get_user_details(user)).email or ""
    # The ORGANIZER comes from the principal's mailto: address, so the user
    # needs a profile email.
    organizer_email = original_email or f"{user}@example.com"
    if not original_email:
        await nc_client.users.update_user_field(user, "email", organizer_email)

    event_uid = None
    try:
        tomorrow = datetime.now() + timedelta(days=1)
        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_event",
            {
                "calendar_name": temporary_calendar,
                "title": "Organizer MCP Test",
                "start_datetime": tomorrow.strftime("%Y-%m-%dT09:00:00"),
                "end_datetime": tomorrow.strftime("%Y-%m-%dT10:00:00"),
                "attendees": "guest@example.com",
            },
        )
        assert create_result.is_error is False, create_result.content
        event_uid = json.loads(create_result.content[0].text)["uid"]

        get_result = await nc_mcp_client.call_tool(
            "nc_calendar_get_event",
            {"calendar_name": temporary_calendar, "event_uid": event_uid},
        )
        assert get_result.is_error is False, get_result.content
        event = json.loads(get_result.content[0].text)
        assert event["organizer"].lower() == organizer_email.lower()
        assert "guest@example.com" in event["attendees"]
    finally:
        if event_uid:
            await nc_client.calendar.delete_event(temporary_calendar, event_uid)
        if not original_email:
            await nc_client.users.update_user_field(user, "email", "")


async def test_mcp_bulk_delete_skips_recurring_series_without_opt_in(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, temporary_calendar: str
):
    """GH #1499: one matched occurrence must not silently delete the whole series."""
    start = (datetime.now() + timedelta(days=7)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    create_result = await nc_mcp_client.call_tool(
        "nc_calendar_create_event",
        {
            "calendar_name": temporary_calendar,
            "title": "Bulk Series MCP Test",
            "start_datetime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_datetime": (start + timedelta(minutes=30)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
            "recurrence_rule": "FREQ=DAILY;COUNT=4",
        },
    )
    assert create_result.is_error is False, create_result.content
    event_uid = json.loads(create_result.content[0].text)["uid"]

    try:
        # The window covers every occurrence, yet the series is one stored
        # event: reported once, and skipped without the opt-in.
        bulk_args = {
            "operation": "delete",
            "calendar_name": temporary_calendar,
            "title_contains": "Bulk Series MCP Test",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": (start + timedelta(days=4)).strftime("%Y-%m-%d"),
        }
        skip_result = await nc_mcp_client.call_tool(
            "nc_calendar_bulk_operations", bulk_args
        )
        assert skip_result.is_error is False, skip_result.content
        skipped = json.loads(skip_result.content[0].text)
        assert skipped["total_found"] == 1
        assert skipped["deleted_count"] == 0
        assert skipped["skipped_count"] == 1
        await nc_client.calendar.get_event(temporary_calendar, event_uid)

        delete_result = await nc_mcp_client.call_tool(
            "nc_calendar_bulk_operations", {**bulk_args, "apply_to_series": True}
        )
        assert delete_result.is_error is False, delete_result.content
        assert json.loads(delete_result.content[0].text)["deleted_count"] == 1
        event_uid = None
    finally:
        if event_uid:
            await nc_client.calendar.delete_event(temporary_calendar, event_uid)
