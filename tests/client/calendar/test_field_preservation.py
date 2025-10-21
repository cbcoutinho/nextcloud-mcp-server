"""Integration tests for CalDAV and CardDAV field preservation.

This test module demonstrates data loss issues when non-supported fields
are present in calendar events and contacts during round-trip operations.
"""

import logging
import uuid
from datetime import datetime, timedelta

import pytest

logger = logging.getLogger(__name__)


@pytest.mark.integration
async def test_calendar_event_custom_fields_preservation(nc_client):
    """Test that custom iCal fields are preserved during round-trip update operations."""
    calendar_name = "personal"

    # Create an event with standard fields
    event_data = {
        "title": "Test Event with Custom Fields",
        "description": "Event to test custom field preservation",
        "start_datetime": (datetime.now() + timedelta(days=1)).isoformat(),
        "end_datetime": (datetime.now() + timedelta(days=1, hours=1)).isoformat(),
        "location": "Test Location",
    }

    # Create the event
    result = await nc_client.calendar.create_event(calendar_name, event_data)
    event_uid = result["uid"]

    try:
        # Get the calendar object from the caldav library
        calendar = nc_client.calendar._get_calendar(calendar_name)
        event = await calendar.event_by_uid(event_uid)
        await event.load()

        # Now manually inject custom iCal properties into the raw data
        # This simulates what would happen if the event was created by another CalDAV client
        # with extended properties
        custom_ical = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test Client//EN
BEGIN:VEVENT
UID:{event_uid}
DTSTART:{(datetime.now() + timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ")}
DTEND:{(datetime.now() + timedelta(days=1, hours=1)).strftime("%Y%m%dT%H%M%SZ")}
SUMMARY:Test Event with Custom Fields
DESCRIPTION:Event to test custom field preservation
LOCATION:Test Location
X-CUSTOM-FIELD:This is a custom field that should be preserved
X-VENDOR-SPECIFIC:Vendor specific data
CATEGORIES:work,testing
STATUS:CONFIRMED
PRIORITY:5
CLASS:PUBLIC
CREATED:{datetime.now().strftime("%Y%m%dT%H%M%SZ")}
DTSTAMP:{datetime.now().strftime("%Y%m%dT%H%M%SZ")}
LAST-MODIFIED:{datetime.now().strftime("%Y%m%dT%H%M%SZ")}
END:VEVENT
END:VCALENDAR"""

        # Update the event's raw data and save
        event.data = custom_ical
        await event.save()

        logger.info(f"Injected custom iCal properties into event {event_uid}")

        # Reload the event to confirm custom fields are present
        await event.load()
        raw_ical_before = event.data

        logger.info("Raw iCal before update:")
        logger.info(raw_ical_before)

        # Verify custom fields exist in raw iCal
        assert (
            "X-CUSTOM-FIELD:This is a custom field that should be preserved"
            in raw_ical_before
        )
        assert "X-VENDOR-SPECIFIC:Vendor specific data" in raw_ical_before

        # Now update the event through the MCP client (simulating normal usage)
        update_data = {
            "title": "Updated Test Event with Custom Fields",
            "description": "Updated description - custom fields should be preserved",
        }

        await nc_client.calendar.update_event(calendar_name, event_uid, update_data)
        logger.info(f"Updated event {event_uid} through MCP client")

        # Reload the event to see if custom fields survived
        await event.load()
        raw_ical_after = event.data

        logger.info("Raw iCal after update:")
        logger.info(raw_ical_after)

        # THIS IS THE CRITICAL TEST - custom fields should be preserved
        assert (
            "X-CUSTOM-FIELD:This is a custom field that should be preserved"
            in raw_ical_after
        ), "Custom field X-CUSTOM-FIELD was lost during round-trip update"

        assert "X-VENDOR-SPECIFIC:Vendor specific data" in raw_ical_after, (
            "Custom field X-VENDOR-SPECIFIC was lost during round-trip update"
        )

        logger.info("✓ Custom fields were preserved during update")

    finally:
        # Cleanup
        try:
            await nc_client.calendar.delete_event(calendar_name, event_uid)
        except Exception as cleanup_error:
            logger.warning(f"Failed to cleanup event {event_uid}: {cleanup_error}")


@pytest.mark.integration
async def test_contact_extended_fields_preservation(nc_client):
    """Test that demonstrates loss of extended vCard fields during round-trip operations."""
    addressbook_name = f"test_preservation_{uuid.uuid4().hex[:8]}"

    # Create a temporary addressbook
    await nc_client.contacts.create_addressbook(
        name=addressbook_name, display_name="Test Preservation Addressbook"
    )

    try:
        contact_uid = str(uuid.uuid4())

        # Create a contact with minimal data first
        basic_contact_data = {
            "fn": "John Extended Doe",
            "email": "john.extended@example.com",
        }

        await nc_client.contacts.create_contact(
            addressbook=addressbook_name,
            uid=contact_uid,
            contact_data=basic_contact_data,
        )

        logger.info(f"Created basic contact {contact_uid}")

        # Now inject a rich vCard with extended fields directly via CardDAV
        extended_vcard = f"""BEGIN:VCARD
VERSION:4.0
UID:{contact_uid}
FN:John Extended Doe
N:Doe;John;Extended;;
NICKNAME:Johnny,JD
EMAIL;TYPE=work:john.work@company.com
EMAIL;TYPE=home:john.extended@example.com
TEL;TYPE=cell:+1-555-123-4567
TEL;TYPE=work:+1-555-987-6543
ADR;TYPE=home:;;123 Main St;Hometown;ST;12345;USA
ADR;TYPE=work:;;456 Work Ave;Worktown;ST;54321;USA
ORG:Example Corporation
TITLE:Senior Developer
URL;TYPE=work:https://company.com/john
URL;TYPE=personal:https://johndoe.dev
BDAY:1985-06-15
NOTE:This is a note with important information that should be preserved.
CATEGORIES:colleagues,developers,friends
X-CUSTOM-FIELD:This should be preserved
X-SKYPE:john.doe.skype
X-LINKEDIN:https://linkedin.com/in/johndoe
REV:{datetime.now().strftime("%Y%m%dT%H%M%SZ")}
END:VCARD"""

        # Direct CardDAV PUT to inject the extended vCard
        contact_path = f"/remote.php/dav/addressbooks/users/{nc_client.contacts.username}/{addressbook_name}/{contact_uid}.vcf"
        await nc_client.contacts._make_request(
            "PUT",
            contact_path,
            content=extended_vcard,
            headers={"Content-Type": "text/vcard; charset=utf-8"},
        )

        logger.info(f"Injected extended vCard for contact {contact_uid}")

        # Retrieve the contact to confirm extended fields are present in raw vCard
        response = await nc_client.contacts._make_request("GET", contact_path)
        raw_vcard_before = response.text

        logger.info("Raw vCard before any operations:")
        logger.info(raw_vcard_before)

        # Verify extended fields exist in raw vCard
        assert "TEL;TYPE=cell:+1-555-123-4567" in raw_vcard_before
        assert "ADR;TYPE=home:;;123 Main St;Hometown;ST;12345;USA" in raw_vcard_before
        assert "ORG:Example Corporation" in raw_vcard_before
        assert "TITLE:Senior Developer" in raw_vcard_before
        assert "X-CUSTOM-FIELD:This should be preserved" in raw_vcard_before
        assert "X-LINKEDIN:https://linkedin.com/in/johndoe" in raw_vcard_before
        assert "NOTE:This is a note with important information" in raw_vcard_before

        # List contacts through the MCP client (this will parse and return limited fields)
        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        our_contact = next((c for c in contacts if c["vcard_id"] == contact_uid), None)

        assert our_contact is not None
        logger.info("Contact as parsed by MCP client:")
        logger.info(our_contact)

        # Check what fields are accessible through the parsed contact
        parsed_contact = our_contact["contact"]

        # These should be available (basic fields that are parsed)
        assert parsed_contact["fullname"] == "John Extended Doe"
        assert parsed_contact["email"] is not None  # Some email should be present

        # The raw vCard should still be available in addressdata
        raw_addressdata = our_contact["addressdata"]
        assert "X-CUSTOM-FIELD:This should be preserved" in raw_addressdata
        assert "ORG:Example Corporation" in raw_addressdata

        # The key test: Can we update this contact without losing extended field data?
        logger.info("Testing contact update preservation...")

        # Update the contact through the MCP client with a simple change
        try:
            await nc_client.contacts.update_contact(
                addressbook=addressbook_name,
                uid=contact_uid,
                contact_data={"email": "john.updated@example.com"},
            )
            logger.info("✓ Contact updated successfully")
        except Exception as e:
            logger.error(f"✗ Failed to update contact: {e}")
            raise

        # Retrieve the contact again to see if extended fields survived
        contacts_after = await nc_client.contacts.list_contacts(
            addressbook=addressbook_name
        )
        updated_contact = next(
            (c for c in contacts_after if c["vcard_id"] == contact_uid), None
        )

        assert updated_contact is not None, "Contact not found after update"
        updated_addressdata = updated_contact["addressdata"]

        logger.info("Raw vCard after contact update:")
        logger.info(updated_addressdata)

        # THIS IS THE CRITICAL TEST - extended fields should be preserved during updates
        extended_field_checks = [
            ("ORG:Example Corporation", "organization field"),
            ("TITLE:Senior Developer", "title field"),
            ("TEL;TYPE=cell:+1-555-123-4567", "cell phone"),
            ("TEL;TYPE=work:+1-555-987-6543", "work phone"),
            ("ADR;TYPE=home:;;123 Main St;Hometown;ST;12345;USA", "home address"),
            ("ADR;TYPE=work:;;456 Work Ave;Worktown;ST;54321;USA", "work address"),
            ("URL;TYPE=work;VALUE=URI:https://company.com/john", "work URL"),
            ("NOTE:This is a note with important information", "note field"),
            ("CATEGORIES:colleagues,developers,friends", "categories"),
            ("X-CUSTOM-FIELD:This should be preserved", "custom field"),
            ("X-LINKEDIN:https://linkedin.com/in/johndoe", "LinkedIn custom field"),
            ("john.updated@example.com", "updated email"),
        ]

        all_preserved = True
        for field_pattern, field_name in extended_field_checks:
            if field_pattern in updated_addressdata:
                logger.info(f"✓ {field_name} preserved")
            else:
                logger.error(f"✗ {field_name} was lost during update")
                all_preserved = False

        # The test should PASS - field preservation should work
        assert all_preserved, (
            "Contact update lost extended field data - this indicates the preservation mechanism failed"
        )

        logger.info("🎉 SUCCESS: All extended fields preserved during contact update!")

    finally:
        # Cleanup
        try:
            await nc_client.contacts.delete_addressbook(name=addressbook_name)
        except Exception as cleanup_error:
            logger.warning(
                f"Failed to cleanup addressbook {addressbook_name}: {cleanup_error}"
            )


@pytest.mark.integration
async def test_calendar_event_roundtrip_data_loss_demonstration(nc_client):
    """Test that extended iCal properties are preserved during round-trip update operations."""
    calendar_name = "personal"

    event_data = {
        "title": "Roundtrip Test Event",
        "description": "Testing data preservation",
        "start_datetime": (datetime.now() + timedelta(days=2)).isoformat(),
        "end_datetime": (datetime.now() + timedelta(days=2, hours=1)).isoformat(),
    }

    result = await nc_client.calendar.create_event(calendar_name, event_data)
    event_uid = result["uid"]

    try:
        # Get the calendar object and event
        calendar = nc_client.calendar._get_calendar(calendar_name)
        event = await calendar.event_by_uid(event_uid)
        await event.load()

        # Inject additional iCal properties that are valid but not supported by our parser
        extended_ical = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Extended Client//EN
BEGIN:VEVENT
UID:{event_uid}
DTSTART:{(datetime.now() + timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")}
DTEND:{(datetime.now() + timedelta(days=2, hours=1)).strftime("%Y%m%dT%H%M%SZ")}
SUMMARY:Roundtrip Test Event
DESCRIPTION:Testing data preservation
STATUS:CONFIRMED
PRIORITY:5
CLASS:PUBLIC
SEQUENCE:1
X-MICROSOFT-CDO-ALLDAYEVENT:FALSE
X-MICROSOFT-CDO-IMPORTANCE:1
X-CUSTOM-MEETING-ID:12345-67890
X-ZOOM-MEETING-URL:https://zoom.us/j/1234567890
ORGANIZER;CN=Test Organizer:mailto:organizer@example.com
COMMENT:This is a comment that should be preserved
LOCATION:Conference Room A
GEO:40.7128;-74.0060
TRANSP:OPAQUE
CREATED:{datetime.now().strftime("%Y%m%dT%H%M%SZ")}
DTSTAMP:{datetime.now().strftime("%Y%m%dT%H%M%SZ")}
LAST-MODIFIED:{datetime.now().strftime("%Y%m%dT%H%M%SZ")}
END:VEVENT
END:VCALENDAR"""

        # Update the event's raw data and save
        event.data = extended_ical
        await event.save()

        # Reload to verify extended properties are present
        await event.load()
        original_ical = event.data

        # Confirm extended properties exist
        extended_properties = [
            "SEQUENCE:1",
            "X-MICROSOFT-CDO-ALLDAYEVENT:FALSE",
            "X-CUSTOM-MEETING-ID:12345-67890",
            "X-ZOOM-MEETING-URL:https://zoom.us/j/1234567890",
            "ORGANIZER;CN=Test Organizer:mailto:organizer@example.com",
            "COMMENT:This is a comment that should be preserved",
            "GEO:40.7128;-74.0060",
            "TRANSP:OPAQUE",
        ]

        # More flexible patterns for properties that might be reformatted
        flexible_patterns = {
            "ORGANIZER;CN=Test Organizer:mailto:organizer@example.com": [
                "ORGANIZER;CN=Test Organizer:mailto:organizer@example.com",
                'ORGANIZER;CN="Test Organizer":mailto:organizer@example.com',
            ],
            "GEO:40.7128;-74.0060": [
                "GEO:40.7128;-74.0060",
                "GEO:40.7128;-74.006",  # May lose trailing zero
            ],
        }

        for prop in extended_properties:
            assert prop in original_ical, (
                f"Extended property {prop} not found in original iCal"
            )

        logger.info("✓ All extended properties confirmed in original iCal")

        # Now perform a simple update through MCP
        update_data = {"location": "Conference Room B"}  # Simple location change
        await nc_client.calendar.update_event(calendar_name, event_uid, update_data)

        # Reload the event to check what survived the round-trip
        await event.load()
        updated_ical = event.data

        logger.info("Checking which properties survived the update...")

        # Check which extended properties survived
        survived = []
        lost = []

        for prop in extended_properties:
            # Check if this property has flexible patterns
            if prop in flexible_patterns:
                # Check if any of the flexible patterns match
                found = any(
                    pattern in updated_ical for pattern in flexible_patterns[prop]
                )
                if found:
                    survived.append(prop)
                else:
                    lost.append(prop)
            else:
                # Standard exact match
                if prop in updated_ical:
                    survived.append(prop)
                else:
                    lost.append(prop)

        logger.info(f"Properties that SURVIVED: {survived}")
        if lost:
            logger.error(f"Properties that were LOST: {lost}")

        # Assert that all extended properties were preserved
        assert len(lost) == 0, (
            f"Round-trip update lost {len(lost)} extended properties: {lost}"
        )

        logger.info("✓ All extended properties preserved during update")

    finally:
        try:
            await nc_client.calendar.delete_event(calendar_name, event_uid)
        except Exception as cleanup_error:
            logger.warning(f"Failed to cleanup event {event_uid}: {cleanup_error}")
