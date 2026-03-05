#!/usr/bin/env python3
"""Test vCalAddress functionality."""

import sys
sys.path.insert(0, '.')

from icalendar import Calendar, Event, vCalAddress, vText

def test_vcaladdress():
    """Test creating vCalAddress objects."""
    # Test attendee
    attendee = vCalAddress("mailto:test@example.com")
    attendee.params['CN'] = vText("Test User")
    attendee.params['ROLE'] = vText('REQ-PARTICIPANT')
    attendee.params['PARTSTAT'] = vText('NEEDS-ACTION')
    attendee.params['CUTYPE'] = vText('INDIVIDUAL')
    attendee.params['RSVP'] = vText('TRUE')
    
    print("Attendee vCalAddress created:")
    print(f"  Value: {attendee}")
    print(f"  Params: {attendee.params}")
    
    # Test organizer
    organizer = vCalAddress("mailto:organizer@example.com")
    organizer.params['CN'] = vText("Organizer Name")
    
    print("\nOrganizer vCalAddress created:")
    print(f"  Value: {organizer}")
    print(f"  Params: {organizer.params}")
    
    # Test string representation
    print(f"\nAttendee string: {str(attendee)}")
    print(f"Organizer string: {str(organizer)}")
    
    # Test email extraction
    attendee_str = str(attendee)
    email = attendee_str.replace("mailto:", "")
    print(f"\nExtracted email from attendee: {email}")
    
    # Test in actual event
    cal = Calendar()
    cal.add("prodid", "-//Test//EN")
    cal.add("version", "2.0")
    
    event = Event()
    event.add("uid", "test-123")
    event.add("summary", "Test Event")
    event['ORGANIZER'] = organizer
    event.add("attendee", attendee)
    
    print("\nEvent created with vCalAddress attendee and organizer")
    
    # Convert to iCal and back
    ical_text = cal.to_ical().decode("utf-8")
    print(f"\nGenerated iCal length: {len(ical_text)}")
    
    # Parse it back
    parsed_cal = Calendar.from_ical(ical_text)
    for component in parsed_cal.walk():
        if component.name == "VEVENT":
            print(f"Parsed event summary: {component.get('summary')}")
            parsed_organizer = component.get('organizer')
            if parsed_organizer:
                print(f"Parsed organizer: {parsed_organizer}")
                print(f"Organizer string: {str(parsed_organizer)}")
            parsed_attendees = component.get('attendee', [])
            print(f"Number of attendees: {len(parsed_attendees)}")
            for i, att in enumerate(parsed_attendees):
                print(f"Attendee {i}: {att}")

if __name__ == "__main__":
    test_vcaladdress()