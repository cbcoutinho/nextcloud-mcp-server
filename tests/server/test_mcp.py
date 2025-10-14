import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


async def test_mcp_connectivity(nc_mcp_client: ClientSession):
    """Test basic MCP server connectivity and list available tools/resources."""

    # List available tools
    tools = await nc_mcp_client.list_tools()
    logger.info("Available MCP tools:")
    tool_names = []
    for tool in tools.tools:
        logger.info(f"  - {tool.name}: {tool.description}")
        tool_names.append(tool.name)

    # Verify expected tools are present
    expected_tools = [
        "nc_notes_create_note",
        "nc_notes_update_note",
        "nc_notes_append_content",
        "nc_notes_search_notes",
        "nc_notes_delete_note",
        "nc_tables_list_tables",
        "nc_tables_get_schema",
        "nc_tables_read_table",
        "nc_tables_insert_row",
        "nc_tables_update_row",
        "nc_tables_delete_row",
        "nc_webdav_list_directory",
        "nc_webdav_read_file",
        "nc_webdav_write_file",
        "nc_webdav_create_directory",
        "nc_webdav_delete_resource",
        "nc_calendar_list_calendars",
        "nc_calendar_create_event",
        "nc_calendar_list_events",
        "nc_calendar_get_event",
        "nc_calendar_update_event",
        "nc_calendar_delete_event",
        "nc_calendar_create_meeting",
        "nc_calendar_get_upcoming_events",
        "nc_calendar_find_availability",
        "nc_calendar_bulk_operations",
        "nc_calendar_manage_calendar",
        "deck_create_board",
    ]

    for expected_tool in expected_tools:
        assert expected_tool in tool_names, (
            f"Expected tool '{expected_tool}' not found in available tools"
        )

    # List available resource templates
    templates = await nc_mcp_client.list_resource_templates()
    logger.info("\nAvailable resource templates:")
    template_uris = []
    for template in templates.resourceTemplates:
        logger.info(f"  - {template.uriTemplate}")
        template_uris.append(template.uriTemplate)

    # Verify expected resource templates
    # Note: Notes attachments are now handled via tools, not resource templates
    expected_templates = []

    for expected_template in expected_templates:
        assert expected_template in template_uris, (
            f"Expected template '{expected_template}' not found"
        )

    # List available resources
    resources = await nc_mcp_client.list_resources()
    logger.info("\nAvailable resources:")
    resource_uris = []
    for resource in resources.resources:
        logger.info(f"  - {resource.uri}: {resource.name}")
        resource_uris.append(str(resource.uri))  # Convert to string for comparison

    # Verify expected resources
    expected_resources = ["nc://capabilities", "notes://settings", "nc://Deck/boards"]

    for expected_resource in expected_resources:
        assert expected_resource in resource_uris, (
            f"Expected resource '{expected_resource}' not found"
        )

    # List available prompts
    prompts = await nc_mcp_client.list_prompts()
    logger.info("\nAvailable prompts:")
    for prompt in prompts.prompts:
        logger.info(f"  - {prompt.name}")


async def test_mcp_notes_crud_workflow(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test complete Notes CRUD workflow via MCP tools with verification via NextcloudClient."""

    unique_suffix = uuid.uuid4().hex[:8]
    test_title = f"MCP Test Note {unique_suffix}"
    test_content = f"This is test content for note {unique_suffix}"
    test_category = "MCPTesting"

    created_note = None

    try:
        # 1. Create note via MCP
        logger.info(f"Creating note via MCP: {test_title}")
        create_result = await nc_mcp_client.call_tool(
            "nc_notes_create_note",
            {"title": test_title, "content": test_content, "category": test_category},
        )

        assert create_result.isError is False, (
            f"MCP note creation failed: {create_result.content}"
        )
        created_note = create_result.content[0].text
        note_data = json.loads(created_note)  # Parse the returned JSON
        note_id = note_data["id"]
        create_etag = note_data["etag"]  # Verify create response includes ETag

        logger.info(f"Note created via MCP with ID: {note_id}, ETag: {create_etag}")
        assert "etag" in note_data, "Create response should include ETag"
        assert create_etag, "Create ETag should not be empty"

        # 2. Verify creation via direct NextcloudClient
        direct_note = await nc_client.notes.get_note(note_id)
        assert direct_note["title"] == test_title, (
            f"Title mismatch: {direct_note['title']} != {test_title}"
        )
        assert direct_note["content"] == test_content, "Content mismatch"
        assert direct_note["category"] == test_category, "Category mismatch"

        # 3. Read note via MCP
        logger.info(f"Reading note via MCP: {note_id}")
        read_result = await nc_mcp_client.call_tool(
            "nc_notes_get_note", {"note_id": note_id}
        )
        read_note_data = read_result.content[0].text
        read_note_data = json.loads(read_note_data)

        assert read_note_data["title"] == test_title
        assert read_note_data["content"] == test_content
        assert read_note_data["category"] == test_category

        # 4. Update note via MCP
        updated_title = f"Updated {test_title}"
        updated_content = f"Updated content: {test_content}"
        etag = read_note_data["etag"]

        logger.info(f"Updating note via MCP: {note_id}")
        update_result = await nc_mcp_client.call_tool(
            "nc_notes_update_note",
            {
                "note_id": note_id,
                "etag": etag,
                "title": updated_title,
                "content": updated_content,
                "category": test_category,
            },
        )

        assert update_result.isError is False, (
            f"MCP note update failed: {update_result.content}"
        )

        # Verify update response includes new ETag
        updated_note_data = json.loads(update_result.content[0].text)
        update_etag = updated_note_data["etag"]

        logger.info(f"Note updated via MCP, new ETag: {update_etag}")
        assert "etag" in updated_note_data, "Update response should include ETag"
        assert update_etag, "Update ETag should not be empty"
        assert update_etag != etag, "ETag should change after update"

        # 5. Verify update via direct NextcloudClient
        updated_direct_note = await nc_client.notes.get_note(note_id)
        assert updated_direct_note["title"] == updated_title
        assert updated_direct_note["content"] == updated_content

        # 6. Append content via MCP
        append_content = "\n\nThis is appended content via MCP."
        logger.info(f"Appending content to note via MCP: {note_id}")
        append_result = await nc_mcp_client.call_tool(
            "nc_notes_append_content", {"note_id": note_id, "content": append_content}
        )

        assert append_result.isError is False, (
            f"MCP note append failed: {append_result.content}"
        )

        # Verify append response includes new ETag
        appended_note_data = json.loads(append_result.content[0].text)
        append_etag = appended_note_data["etag"]

        logger.info(f"Content appended via MCP, new ETag: {append_etag}")
        assert "etag" in appended_note_data, "Append response should include ETag"
        assert append_etag, "Append ETag should not be empty"
        assert append_etag != update_etag, "ETag should change after append"

        # 7. Verify append via direct NextcloudClient
        appended_direct_note = await nc_client.notes.get_note(note_id)
        assert append_content in appended_direct_note["content"]

        # 8. Search for note via MCP
        logger.info(f"Searching for note via MCP with query: {unique_suffix}")
        search_result = await nc_mcp_client.call_tool(
            "nc_notes_search_notes", {"query": unique_suffix}
        )

        assert search_result.isError is False, (
            f"MCP note search failed: {search_result.content}"
        )
        search_notes_text = search_result.content[0].text
        logger.info(f"Search result text: {search_notes_text}")
        search_response = json.loads(search_notes_text)

        # Expect structured response with Pydantic format
        assert isinstance(search_response, dict), (
            f"Expected search response to be a dict with structured format, got: {type(search_response)}"
        )
        assert "results" in search_response, (
            f"Expected 'results' field in search response, got keys: {list(search_response.keys())}"
        )
        assert "success" in search_response and search_response["success"], (
            f"Expected successful search response, got: {search_response}"
        )

        search_notes = search_response["results"]
        assert isinstance(search_notes, list), (
            f"Expected results to be a list, got: {type(search_notes)}"
        )

        # Find our note in search results
        found_note = None
        for note in search_notes:
            if isinstance(note, dict) and note.get("id") == note_id:
                found_note = note
                break

        assert found_note is not None, (
            f"Created note not found in search results. Search returned: {search_response}"
        )
        assert found_note["title"] == updated_title

        # 9. Delete note via MCP
        logger.info(f"Deleting note via MCP: {note_id}")
        delete_result = await nc_mcp_client.call_tool(
            "nc_notes_delete_note", {"note_id": note_id}
        )

        assert delete_result.isError is False, (
            f"MCP note deletion failed: {delete_result.content}"
        )

        # 10. Verify deletion via direct NextcloudClient
        try:
            await nc_client.notes.get_note(note_id)
            pytest.fail("Note should have been deleted but was still found")
        except Exception:
            # Expected - note should be deleted
            logger.info(f"Successfully verified note {note_id} was deleted")
            created_note = None  # Mark as cleaned up

    finally:
        # Cleanup in case of test failure
        if created_note is not None:
            try:
                note_data = json.loads(created_note)
                await nc_client.notes.delete_note(note_data["id"])
                logger.info(f"Cleaned up note {note_data['id']} after test failure")
            except Exception as e:
                logger.warning(f"Failed to cleanup note: {e}")


async def test_mcp_notes_etag_conflict(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test that MCP note updates fail when using stale ETags."""

    unique_suffix = uuid.uuid4().hex[:8]
    test_title = f"ETag Test Note {unique_suffix}"
    test_content = f"This is test content for ETag testing {unique_suffix}"
    test_category = "ETTesting"

    created_note = None

    try:
        # 1. Create note via MCP
        logger.info(f"Creating note for ETag conflict test: {test_title}")
        create_result = await nc_mcp_client.call_tool(
            "nc_notes_create_note",
            {"title": test_title, "content": test_content, "category": test_category},
        )

        assert create_result.isError is False
        note_data = json.loads(create_result.content[0].text)
        note_id = note_data["id"]
        original_etag = note_data["etag"]
        created_note = note_data

        # 2. Update note to change ETag
        first_update_result = await nc_mcp_client.call_tool(
            "nc_notes_update_note",
            {
                "note_id": note_id,
                "etag": original_etag,
                "title": f"First Update {test_title}",
                "content": test_content,
                "category": test_category,
            },
        )

        assert first_update_result.isError is False
        updated_data = json.loads(first_update_result.content[0].text)
        new_etag = updated_data["etag"]
        assert new_etag != original_etag, "ETag should have changed after update"

        # 3. Try to update with the stale (original) ETag - this should fail
        logger.info(f"Attempting update with stale ETag: {original_etag}")
        conflict_result = await nc_mcp_client.call_tool(
            "nc_notes_update_note",
            {
                "note_id": note_id,
                "etag": original_etag,  # Use stale ETag
                "title": "This should fail",
                "content": "This update should be rejected",
                "category": test_category,
            },
        )

        # 4. Verify the update was rejected with a 412 error
        # With McpError, tools now return proper error responses
        assert conflict_result.isError is True, "Update with stale ETag should fail"
        response_content = conflict_result.content[0].text
        assert "modified by someone else" in response_content, (
            f"Expected conflict error message, got: {response_content}"
        )

        logger.info("Successfully verified ETag conflict handling via MCP")

    finally:
        # Cleanup
        if created_note is not None:
            try:
                await nc_client.notes.delete_note(created_note["id"])
                logger.info(f"Cleaned up test note {created_note['id']}")
            except Exception as e:
                logger.warning(f"Failed to cleanup test note: {e}")


async def test_mcp_webdav_workflow(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test WebDAV file operations via MCP tools with verification via NextcloudClient."""

    unique_suffix = uuid.uuid4().hex[:8]
    test_dir = f"mcp_test_dir_{unique_suffix}"
    test_file = f"mcp_test_file_{unique_suffix}.txt"
    test_file_path = f"{test_dir}/{test_file}"
    test_content = f"This is test content for MCP WebDAV testing {unique_suffix}"

    try:
        # 1. Create directory via MCP
        logger.info(f"Creating directory via MCP: {test_dir}")
        create_dir_result = await nc_mcp_client.call_tool(
            "nc_webdav_create_directory", {"path": test_dir}
        )

        assert create_dir_result.isError is False, (
            f"MCP directory creation failed: {create_dir_result.content}"
        )

        # 2. Verify directory creation via direct WebDAV
        dir_listing = await nc_client.webdav.list_directory("")
        dir_names = [item["name"] for item in dir_listing if item["is_directory"]]
        assert test_dir in dir_names, f"Directory {test_dir} not found in root listing"

        # 3. Write file via MCP
        logger.info(f"Writing file via MCP: {test_file_path}")
        write_result = await nc_mcp_client.call_tool(
            "nc_webdav_write_file",
            {
                "path": test_file_path,
                "content": test_content,
                "content_type": "text/plain",
            },
        )

        assert write_result.isError is False, (
            f"MCP file write failed: {write_result.content}"
        )

        # 4. Verify file creation via direct WebDAV
        file_listing = await nc_client.webdav.list_directory(test_dir)
        file_names = [item["name"] for item in file_listing if not item["is_directory"]]
        assert test_file in file_names, (
            f"File {test_file} not found in directory listing"
        )

        # 5. Read file via MCP
        logger.info(f"Reading file via MCP: {test_file_path}")
        read_result = await nc_mcp_client.call_tool(
            "nc_webdav_read_file", {"path": test_file_path}
        )

        assert read_result.isError is False, (
            f"MCP file read failed: {read_result.content}"
        )
        read_data = json.loads(read_result.content[0].text)

        assert read_data["content"] == test_content, "File content mismatch"
        assert read_data["path"] == test_file_path
        assert "text/plain" in read_data["content_type"]

        # 6. Verify file content via direct WebDAV
        direct_content, direct_content_type = await nc_client.webdav.read_file(
            test_file_path
        )
        assert direct_content.decode("utf-8") == test_content

        # 7. List directory via MCP
        logger.info(f"Listing directory via MCP: {test_dir}")
        list_result = await nc_mcp_client.call_tool(
            "nc_webdav_list_directory", {"path": test_dir}
        )

        assert list_result.isError is False, (
            f"MCP directory listing failed: {list_result.content}"
        )
        listing_text = list_result.content[0].text
        logger.info(f"Directory listing response: {listing_text}")
        listing_data = json.loads(listing_text)

        # Ensure listing_data is a list
        if not isinstance(listing_data, list):
            logger.warning(
                f"Expected directory listing to be a list, got: {type(listing_data)}"
            )
            listing_data = [listing_data] if listing_data else []

        # Find our file in the listing
        found_file = None
        for item in listing_data:
            if isinstance(item, dict) and item.get("name") == test_file:
                found_file = item
                break

        assert found_file is not None, (
            f"File {test_file} not found in MCP directory listing"
        )
        assert found_file["is_directory"] is False
        assert found_file["size"] == len(test_content.encode("utf-8"))

    finally:
        # Cleanup
        try:
            logger.info(f"Cleaning up test file: {test_file_path}")
            await nc_mcp_client.call_tool(
                "nc_webdav_delete_resource", {"path": test_file_path}
            )

            logger.info(f"Cleaning up test directory: {test_dir}")
            await nc_mcp_client.call_tool(
                "nc_webdav_delete_resource", {"path": test_dir}
            )
        except Exception as e:
            logger.warning(f"Failed to cleanup WebDAV resources: {e}")


async def test_mcp_resources_access(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test accessing MCP resources and compare with direct API calls."""

    # 1. Test capabilities resource
    logger.info("Testing capabilities resource via MCP")
    caps_result = await nc_mcp_client.read_resource("nc://capabilities")
    assert len(caps_result.contents) == 1
    mcp_capabilities = json.loads(caps_result.contents[0].text)

    # Compare with direct API call
    direct_capabilities = await nc_client.capabilities()

    # Basic validation - both should have similar structure
    # Both return full OCS response structure
    assert "ocs" in mcp_capabilities
    assert "data" in mcp_capabilities["ocs"]
    assert "version" in mcp_capabilities["ocs"]["data"]
    assert "ocs" in direct_capabilities
    assert "data" in direct_capabilities["ocs"]
    assert "version" in direct_capabilities["ocs"]["data"]

    # 2. Test notes settings resource
    logger.info("Testing notes settings resource via MCP")
    settings_result = await nc_mcp_client.read_resource("notes://settings")
    assert len(settings_result.contents) == 1
    mcp_settings = json.loads(settings_result.contents[0].text)

    # Compare with direct API call
    direct_settings = await nc_client.notes.get_settings()

    # Both should have settings data
    assert isinstance(mcp_settings, dict)
    assert isinstance(direct_settings, dict)

    logger.info("Successfully verified MCP resources match direct API calls")


async def test_mcp_calendar_workflow(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test complete Calendar workflow via MCP tools with verification via NextcloudClient."""

    unique_suffix = uuid.uuid4().hex[:8]
    test_event_title = f"MCP Test Event {unique_suffix}"
    test_location = f"MCP Test Location {unique_suffix}"

    created_event = None
    calendar_name = None

    try:
        # 1. List calendars via MCP
        logger.info("Listing calendars via MCP")
        calendars_result = await nc_mcp_client.call_tool(
            "nc_calendar_list_calendars", {}
        )

        assert calendars_result.isError is False, (
            f"MCP calendar listing failed: {calendars_result.content}"
        )

        calendars_response = json.loads(calendars_result.content[0].text)

        # Debug output to understand the structure
        logger.info(f"calendars_response type: {type(calendars_response)}")
        logger.info(f"calendars_response content: {calendars_response}")

        # Expect structured response with Pydantic format
        assert isinstance(calendars_response, dict), (
            f"Expected calendar response to be a dict with structured format, got: {type(calendars_response)}"
        )
        assert "calendars" in calendars_response, (
            f"Expected 'calendars' field in response, got keys: {list(calendars_response.keys())}"
        )
        assert "success" in calendars_response and calendars_response["success"], (
            f"Expected successful calendar response, got: {calendars_response}"
        )

        calendars_list = calendars_response["calendars"]
        assert isinstance(calendars_list, list), (
            f"Expected calendars to be a list, got: {type(calendars_list)}"
        )

        if not calendars_list:
            pytest.skip("No calendars available for testing")

        # Use the first available calendar
        calendar_name = calendars_list[0]["name"]
        logger.info(f"Using calendar: {calendar_name}")

        # 2. Create event via MCP
        from datetime import datetime, timedelta

        tomorrow = datetime.now() + timedelta(days=1)
        start_datetime = tomorrow.strftime("%Y-%m-%dT14:00:00")
        end_datetime = tomorrow.strftime("%Y-%m-%dT15:00:00")

        event_data = {
            "calendar_name": calendar_name,
            "title": test_event_title,
            "start_datetime": start_datetime,
            "end_datetime": end_datetime,
            "description": f"Test event created via MCP {unique_suffix}",
            "location": test_location,
            "categories": "testing,mcp",
            "status": "CONFIRMED",
            "priority": 5,
        }

        logger.info(f"Creating event via MCP: {test_event_title}")
        create_result = await nc_mcp_client.call_tool(
            "nc_calendar_create_event", event_data
        )

        assert create_result.isError is False, (
            f"MCP event creation failed: {create_result.content}"
        )

        created_event_data = json.loads(create_result.content[0].text)
        event_uid = created_event_data["uid"]
        created_event = {"uid": event_uid, "calendar_name": calendar_name}

        logger.info(f"Event created via MCP with UID: {event_uid}")

        # 3. Verify creation via direct NextcloudClient
        direct_event, _ = await nc_client.calendar.get_event(calendar_name, event_uid)
        assert direct_event["title"] == test_event_title
        assert direct_event["location"] == test_location
        assert "testing" in direct_event.get("categories", "")

        # 4. Get event via MCP
        logger.info(f"Getting event via MCP: {event_uid}")
        get_result = await nc_mcp_client.call_tool(
            "nc_calendar_get_event",
            {"calendar_name": calendar_name, "event_uid": event_uid},
        )

        assert get_result.isError is False, (
            f"MCP event get failed: {get_result.content}"
        )

        get_event_data = json.loads(get_result.content[0].text)
        assert get_event_data["title"] == test_event_title
        assert get_event_data["location"] == test_location

        # 5. **TEST nc_calendar_list_events - This is the main tool we're testing**
        logger.info("Testing nc_calendar_list_events via MCP")

        # Get today and next week for date range
        today = datetime.now()
        next_week = today + timedelta(days=7)
        start_date = today.strftime("%Y-%m-%d")
        end_date = next_week.strftime("%Y-%m-%d")

        list_events_data = {
            "calendar_name": calendar_name,
            "start_date": start_date,
            "end_date": end_date,
            "limit": 50,
            "location_contains": "MCP Test",
            "title_contains": unique_suffix,
        }

        list_result = await nc_mcp_client.call_tool(
            "nc_calendar_list_events", list_events_data
        )

        assert list_result.isError is False, (
            f"MCP list events failed: {list_result.content}"
        )

        events_data = json.loads(list_result.content[0].text)

        # Debug output to understand what nc_calendar_list_events returns
        logger.info(f"list_events result type: {type(events_data)}")
        logger.info(f"list_events result content: {events_data}")

        # Handle single event returned as dict instead of list (same fix as calendars)
        if isinstance(events_data, dict):
            # Single event returned as dict instead of list
            events_data = [events_data]

        assert isinstance(events_data, list), "Expected events list"

        # Our created event should be in the list
        found_event = None
        for event in events_data:
            if event.get("uid") == event_uid:
                found_event = event
                break

        assert found_event is not None, (
            f"Created event {event_uid} not found in events list"
        )
        assert found_event["title"] == test_event_title

        # 6. Test list events across all calendars
        logger.info("Testing nc_calendar_list_events across all calendars")

        all_calendars_data = {
            "calendar_name": "",  # Will be ignored
            "search_all_calendars": True,
            "start_date": start_date,
            "end_date": end_date,
            "title_contains": unique_suffix,
        }

        all_list_result = await nc_mcp_client.call_tool(
            "nc_calendar_list_events", all_calendars_data
        )

        assert all_list_result.isError is False, (
            f"MCP list all events failed: {all_list_result.content}"
        )

        all_events_data = json.loads(all_list_result.content[0].text)

        # Handle single event returned as dict instead of list (same fix as calendars)
        if isinstance(all_events_data, dict):
            # Single event returned as dict instead of list
            all_events_data = [all_events_data]

        assert isinstance(all_events_data, list), "Expected events list"

        # Our event should still be found when searching all calendars
        found_in_all = any(event.get("uid") == event_uid for event in all_events_data)
        assert found_in_all, "Event not found when searching all calendars"

        # 7. Update event via MCP
        updated_title = f"Updated {test_event_title}"
        updated_description = f"Updated description {unique_suffix}"

        update_data = {
            "calendar_name": calendar_name,
            "event_uid": event_uid,
            "title": updated_title,
            "description": updated_description,
            "priority": 1,
        }

        logger.info(f"Updating event via MCP: {event_uid}")
        update_result = await nc_mcp_client.call_tool(
            "nc_calendar_update_event", update_data
        )

        assert update_result.isError is False, (
            f"MCP event update failed: {update_result.content}"
        )

        # 8. Verify update via direct NextcloudClient
        updated_direct_event, _ = await nc_client.calendar.get_event(
            calendar_name, event_uid
        )
        assert updated_direct_event["title"] == updated_title
        assert updated_direct_event["description"] == updated_description
        assert updated_direct_event["priority"] == 1

        # 9. Test upcoming events via MCP
        logger.info("Testing nc_calendar_get_upcoming_events via MCP")
        upcoming_result = await nc_mcp_client.call_tool(
            "nc_calendar_get_upcoming_events",
            {"calendar_name": calendar_name, "days_ahead": 7, "limit": 10},
        )

        assert upcoming_result.isError is False, (
            f"MCP upcoming events failed: {upcoming_result.content}"
        )

        upcoming_events = json.loads(upcoming_result.content[0].text)

        # Handle single event returned as dict instead of list (same fix as other tools)
        if isinstance(upcoming_events, dict):
            # Single event returned as dict instead of list
            upcoming_events = [upcoming_events]

        assert isinstance(upcoming_events, list), "Expected upcoming events list"

        # 10. Delete event via MCP
        logger.info(f"Deleting event via MCP: {event_uid}")
        delete_result = await nc_mcp_client.call_tool(
            "nc_calendar_delete_event",
            {"calendar_name": calendar_name, "event_uid": event_uid},
        )

        assert delete_result.isError is False, (
            f"MCP event deletion failed: {delete_result.content}"
        )

        # 11. Verify deletion via direct NextcloudClient
        try:
            await nc_client.calendar.get_event(calendar_name, event_uid)
            pytest.fail("Event should have been deleted but was still found")
        except Exception:
            # Expected - event should be deleted
            logger.info(f"Successfully verified event {event_uid} was deleted")
            created_event = None  # Mark as cleaned up

    except Exception as e:
        if "Calendar app may not be enabled" in str(
            e
        ) or "No calendars available" in str(e):
            pytest.skip("Calendar functionality not available for testing")
        raise

    finally:
        # Cleanup in case of test failure
        if created_event is not None:
            try:
                await nc_client.calendar.delete_event(
                    created_event["calendar_name"], created_event["uid"]
                )
                logger.info(
                    f"Cleaned up event {created_event['uid']} after test failure"
                )
            except Exception as e:
                logger.warning(f"Failed to cleanup event: {e}")
