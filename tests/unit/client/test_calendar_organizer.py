#!/usr/bin/env python3
"""Test organizer default logic."""

import sys
sys.path.insert(0, '.')

from nextcloud_mcp_server.client.calendar import CalendarClient
from httpx import BasicAuth

def test_parse_name_email():
    """Test parsing of name/email strings."""
    # Mock client just for testing the method
    client = CalendarClient("http://example.com", "testuser")
    
    # Test email only
    name, email = client._parse_name_email("alice@example.com")
    assert name == "alice"
    assert email == "alice@example.com"
    
    # Test name <email>
    name, email = client._parse_name_email("Alice Smith <alice@example.com>")
    assert name == "Alice Smith"
    assert email == "alice@example.com"
    
    # Test with extra spaces
    name, email = client._parse_name_email("  Bob Jones  <bob@example.com>  ")
    assert name == "Bob Jones"
    assert email == "bob@example.com"
    
    # Test just name (no email) - should use name as email
    name, email = client._parse_name_email("charlie")
    assert name == "charlie"
    assert email == "charlie"
    
    print("✓ _parse_name_email tests passed")

def test_default_organizer():
    """Test default organizer generation."""
    # Test with no user info, with hostname
    client = CalendarClient("https://cloud.example.com", "testuser")
    organizer = client._get_default_organizer()
    print(f"Default organizer with hostname: {organizer}")
    # Should be "testuser <testuser@cloud.example.com>" or similar
    assert organizer is not None
    assert "testuser" in organizer
    assert "@" in organizer
    
    # Test with user email provided
    client2 = CalendarClient(
        "https://cloud.example.com", 
        "testuser",
        user_email="user@example.com"
    )
    organizer2 = client2._get_default_organizer()
    print(f"Default organizer with email: {organizer2}")
    assert organizer2 == "testuser <user@example.com>"
    
    # Test with user display name and email
    client3 = CalendarClient(
        "https://cloud.example.com",
        "testuser",
        user_email="user@example.com",
        user_display_name="Test User"
    )
    organizer3 = client3._get_default_organizer()
    print(f"Default organizer with name and email: {organizer3}")
    assert organizer3 == "Test User <user@example.com>"
    
    # Test with only display name (no email)
    client4 = CalendarClient(
        "https://cloud.example.com",
        "testuser",
        user_display_name="Test User"
    )
    organizer4 = client4._get_default_organizer()
    print(f"Default organizer with name only: {organizer4}")
    assert organizer4 is not None
    assert "Test User" in organizer4
    assert "@" in organizer4
    
    # Test with no hostname (invalid URL)
    client5 = CalendarClient("not-a-url", "testuser")
    organizer5 = client5._get_default_organizer()
    print(f"Default organizer with no hostname: {organizer5}")
    # Should return None because no email can be constructed
    assert organizer5 is None
    
    print("✓ _get_default_organizer tests passed")

def test_vcal_creation():
    """Test vCalAddress creation."""
    from icalendar import vCalAddress, vText
    
    client = CalendarClient("https://cloud.example.com", "testuser")
    
    # Test attendee vCal
    attendee = client._create_attendee_vcal("Alice <alice@example.com>")
    assert isinstance(attendee, vCalAddress)
    assert attendee.params['CN'] == vText('Alice')
    assert attendee.params['ROLE'] == vText('REQ-PARTICIPANT')
    assert attendee.params['PARTSTAT'] == vText('NEEDS-ACTION')
    assert attendee.params['CUTYPE'] == vText('INDIVIDUAL')
    assert attendee.params['RSVP'] == vText('TRUE')
    assert str(attendee) == "mailto:alice@example.com"
    
    # Test organizer vCal
    organizer = client._create_organizer_vcal("Organizer <org@example.com>")
    assert isinstance(organizer, vCalAddress)
    assert organizer.params['CN'] == vText('Organizer')
    assert str(organizer) == "mailto:org@example.com"
    
    print("✓ vCalAddress creation tests passed")

if __name__ == "__main__":
    try:
        test_parse_name_email()
        test_default_organizer()
        test_vcal_creation()
        print("\n✅ All tests passed!")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)