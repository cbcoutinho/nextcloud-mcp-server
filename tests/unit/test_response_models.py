"""Unit tests for Pydantic response models."""

from datetime import date, datetime

import pytest

from nextcloud_mcp_server.models.contacts import (
    Contact,
    ListContactsResponse,
)
from nextcloud_mcp_server.models.notes import (
    CreateNoteResponse,
    Note,
    NoteSearchResult,
    SearchNotesResponse,
)
from nextcloud_mcp_server.models.tables import Table, TableSchema
from nextcloud_mcp_server.server.calendar import _event_dict_to_summary
from nextcloud_mcp_server.server.contacts import _raw_contact_to_model


@pytest.mark.unit
def test_note_model_creation():
    """Test creating a Note model with required fields."""
    note = Note(
        id=123,
        title="Test Note",
        content="# Test Content",
        modified=1700000000,
        etag="abc123",
    )

    assert note.id == 123
    assert note.title == "Test Note"
    assert note.content == "# Test Content"
    assert note.category == ""  # default value
    assert note.favorite is False  # default value
    assert note.etag == "abc123"


@pytest.mark.unit
def test_note_modified_datetime_property():
    """Test that Note.modified_datetime converts Unix timestamp correctly."""
    note = Note(
        id=1,
        title="Test",
        content="Content",
        modified=1700000000,
        etag="etag",
    )

    dt = note.modified_datetime
    assert dt.year == 2023  # Nov 14, 2023
    assert dt.month == 11


@pytest.mark.unit
def test_create_note_response_serialization():
    """Test CreateNoteResponse can serialize to JSON."""
    response = CreateNoteResponse(
        id=42,
        title="New Note",
        category="Work",
        etag="xyz789",
    )

    # Test serialization
    data = response.model_dump()
    assert data["id"] == 42
    assert data["title"] == "New Note"
    assert data["category"] == "Work"
    assert data["etag"] == "xyz789"


@pytest.mark.unit
def test_search_notes_response_wraps_results():
    """Test SearchNotesResponse wraps list of results correctly.

    This is critical - MCPServer mangles raw List[Dict] responses,
    so we must wrap them in a response model.
    """
    results = [
        NoteSearchResult(id=1, title="First Note", category="Work"),
        NoteSearchResult(id=2, title="Second Note", category="Personal"),
    ]

    response = SearchNotesResponse(
        results=results,
        query="test query",
        total_found=2,
    )

    # Verify the response structure
    assert len(response.results) == 2
    assert response.results[0].id == 1
    assert response.results[1].title == "Second Note"
    assert response.query == "test query"
    assert response.total_found == 2

    # Verify it serializes correctly
    data = response.model_dump()
    assert "results" in data
    assert isinstance(data["results"], list)
    assert len(data["results"]) == 2
    assert data["results"][0]["id"] == 1


@pytest.mark.unit
def test_note_search_result_with_score():
    """Test NoteSearchResult with optional score field."""
    result = NoteSearchResult(
        id=99,
        title="Relevant Note",
        category="Archive",
        score=0.95,
    )

    assert result.id == 99
    assert result.score == 0.95


@pytest.mark.unit
def test_note_search_result_without_score():
    """Test NoteSearchResult without optional score field."""
    result = NoteSearchResult(
        id=99,
        title="Relevant Note",
        category="Archive",
    )

    assert result.id == 99
    assert result.score is None


def _map_contact(raw: dict) -> Contact:
    """Thin wrapper around the production mapping function for test readability."""
    return _raw_contact_to_model(raw)


@pytest.mark.unit
def test_contact_mapping_preserves_email_birthday_nickname():
    """Test that list_contacts mapping preserves email, birthday, and nickname.

    Regression test for PR #574: the original mapping only kept uid, fn, etag
    and silently dropped email, birthday, and nickname.
    """
    raw_contact = {
        "vcard_id": "abc-123",
        "getetag": '"etag-val"',
        "contact": {
            "fullname": "Jane Doe",
            "email": "jane@example.com",
            "birthday": "1990-05-15",
            "nickname": "JD",
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.uid == "abc-123"
    assert contact.fn == "Jane Doe"
    assert contact.etag == '"etag-val"'
    assert contact.birthday == "1990-05-15"
    assert len(contact.emails) == 1
    assert contact.emails[0].value == "jane@example.com"
    assert contact.emails[0].type == "email"
    assert contact.custom_fields["nickname"] == "JD"


@pytest.mark.unit
def test_contact_mapping_exposes_resource_path():
    """Issue #874: the real CardDAV object path must surface on the model so
    callers can address objects whose filename isn't ``<uid>.vcf``.
    """
    raw_contact = {
        "vcard_id": "default",
        "object_path": "/remote.php/dav/addressbooks/users/admin/contacts/default",
        "object_name": "default",
        "getetag": '"etag-val"',
        "contact": {"fullname": "No Ext"},
    }

    contact = _map_contact(raw_contact)

    assert contact.uid == "default"
    assert (
        contact.resource_path
        == "/remote.php/dav/addressbooks/users/admin/contacts/default"
    )


@pytest.mark.unit
def test_contact_mapping_resource_path_optional():
    """Mapping must not require ``object_path`` (older callers / partial dicts)."""
    contact = _map_contact({"vcard_id": "x", "contact": {"fullname": "X"}})
    assert contact.resource_path is None


@pytest.mark.unit
def test_contact_mapping_birthday_datetime_date_object():
    """Test that a datetime.date birthday is converted to ISO string.

    Regression test for GH #672: pythonvCard4 returns datetime.date objects
    for BDAY fields, which caused Pydantic validation errors.
    """
    raw_contact = {
        "vcard_id": "bday-date-1",
        "contact": {
            "fullname": "Date Object",
            "birthday": date(1990, 5, 15),
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.birthday == "1990-05-15"


@pytest.mark.unit
def test_contact_mapping_birthday_apple_unknown_year():
    """Test Apple/iOS unknown-year birthday convention (year 1604).

    Apple contacts use BDAY;VALUE=DATE:16040808 when the birth year is unknown.
    pythonvCard4 parses this as datetime.date(1604, 8, 8).
    """
    raw_contact = {
        "vcard_id": "bday-apple-1",
        "contact": {
            "fullname": "Apple Contact",
            "birthday": date(1604, 8, 8),
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.birthday == "1604-08-08"


@pytest.mark.unit
def test_contact_mapping_multiple_emails():
    """Test that multiple emails are mapped correctly."""
    raw_contact = {
        "vcard_id": "def-456",
        "contact": {
            "fullname": "John Smith",
            "email": ["john@work.com", "john@home.com"],
        },
    }

    contact = _map_contact(raw_contact)

    assert len(contact.emails) == 2
    assert contact.emails[0].value == "john@work.com"
    assert contact.emails[1].value == "john@home.com"


@pytest.mark.unit
def test_contact_mapping_missing_optional_fields():
    """Test mapping when email, birthday, and nickname are absent."""
    raw_contact = {
        "vcard_id": "ghi-789",
        "contact": {"fullname": "No Details"},
    }

    contact = _map_contact(raw_contact)

    assert contact.uid == "ghi-789"
    assert contact.fn == "No Details"
    assert contact.birthday is None
    assert contact.emails == []
    assert contact.custom_fields == {}


@pytest.mark.unit
def test_contact_mapping_surfaces_org_title_note_url_categories_photo():
    """Issue #716 follow-up: ORG / TITLE / NOTE / URL / CATEGORIES / PHOTO must
    round-trip from the raw client dict onto the response model.

    Before this fix, the server-side mapper ignored these keys even when the
    client supplied them, so MCP responses returned ``organization: null`` /
    ``note: null`` for contacts that did have the fields set in their vCard.
    """
    raw_contact = {
        "vcard_id": "full-1",
        "getetag": '"etag"',
        "contact": {
            "fullname": "Alice",
            "org": "Acme Corp",
            "title": "Engineer",
            "note": "Met at conference",
            "url": ["https://acme.example.com"],
            "categories": ["vip", "customer"],
            "photo": "https://photos.example.com/alice.jpg",
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.organization == "Acme Corp"
    assert contact.title == "Engineer"
    assert contact.note == "Met at conference"
    assert len(contact.urls) == 1
    assert contact.urls[0].value == "https://acme.example.com"
    assert contact.urls[0].type == "url"
    assert contact.categories == ["vip", "customer"]
    assert contact.photo == "https://photos.example.com/alice.jpg"


@pytest.mark.unit
def test_contact_mapping_accepts_url_as_plain_string():
    """The client dict normally carries ``url`` as a list, but accept a plain
    string for forward-compat with library changes that might collapse single
    entries.
    """
    raw_contact = {
        "vcard_id": "url-str-1",
        "contact": {"fullname": "Bob", "url": "https://bob.example.com"},
    }

    contact = _map_contact(raw_contact)

    assert len(contact.urls) == 1
    assert contact.urls[0].value == "https://bob.example.com"


@pytest.mark.unit
def test_contact_mapping_categories_accepts_comma_string():
    """A comma-separated string is split into discrete categories on the read
    side, matching how the write side parses ``categories``.
    """
    raw_contact = {
        "vcard_id": "cat-1",
        "contact": {"fullname": "Carol", "categories": "vip, customer, archived"},
    }

    contact = _map_contact(raw_contact)

    assert contact.categories == ["vip", "customer", "archived"]


@pytest.mark.unit
def test_list_contacts_response_wraps_contacts():
    """Test ListContactsResponse wraps contacts correctly for MCP output."""
    contacts = [
        _map_contact(
            {
                "vcard_id": "a",
                "getetag": '"e1"',
                "contact": {
                    "fullname": "Alice",
                    "email": "alice@test.com",
                    "birthday": "2000-01-01",
                    "nickname": "Ali",
                },
            }
        ),
    ]

    response = ListContactsResponse(
        contacts=contacts, addressbook="personal", total_count=1
    )

    data = response.model_dump()
    assert data["total_count"] == 1
    assert len(data["contacts"]) == 1
    c = data["contacts"][0]
    assert c["birthday"] == "2000-01-01"
    assert c["emails"][0]["value"] == "alice@test.com"
    assert c["custom_fields"]["nickname"] == "Ali"


@pytest.mark.unit
def test_contact_mapping_dict_format_emails():
    """Regression for #601: pythonvCard4 returns dicts, not plain strings."""
    raw_contact = {
        "vcard_id": "dict-email-1",
        "contact": {
            "fullname": "Evrim Yilmaz",
            "email": [
                {"value": "evrim@example.com", "type": ["HOME"]},
                {"value": "evrim@work.com", "type": ["WORK"]},
            ],
        },
    }

    contact = _map_contact(raw_contact)

    assert len(contact.emails) == 2
    assert contact.emails[0].value == "evrim@example.com"
    assert contact.emails[0].label == "home"
    assert contact.emails[1].value == "evrim@work.com"
    assert contact.emails[1].label == "work"


@pytest.mark.unit
def test_contact_mapping_dict_format_phones():
    """Phones from dict-format tel field are parsed into Contact.phones."""
    raw_contact = {
        "vcard_id": "dict-tel-1",
        "contact": {
            "fullname": "Phone User",
            "tel": [
                {"value": "+1-555-0100", "type": ["CELL"]},
                {"value": "+1-555-0200", "type": ["WORK", "VOICE"]},
            ],
        },
    }

    contact = _map_contact(raw_contact)

    assert len(contact.phones) == 2
    assert contact.phones[0].value == "+1-555-0100"
    assert contact.phones[0].type == "phone"
    assert contact.phones[0].label == "cell"
    assert contact.phones[1].value == "+1-555-0200"
    assert contact.phones[1].label == "work, voice"


@pytest.mark.unit
def test_contact_mapping_pref_flag_extraction():
    """PREF type is extracted as preferred=True, not included in labels."""
    raw_contact = {
        "vcard_id": "pref-1",
        "contact": {
            "fullname": "Pref User",
            "email": [
                {"value": "pref@example.com", "type": ["HOME", "PREF"]},
                {"value": "other@example.com", "type": ["WORK"]},
            ],
            "tel": [
                {"value": "+1-555-0001", "type": ["pref", "CELL"]},
            ],
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.emails[0].preferred is True
    assert contact.emails[0].label == "home"  # PREF stripped from label
    assert contact.emails[1].preferred is False
    assert contact.primary_email == "pref@example.com"

    assert contact.phones[0].preferred is True
    assert contact.phones[0].label == "cell"
    assert contact.primary_phone == "+1-555-0001"


@pytest.mark.unit
def test_contact_mapping_backward_compat_plain_strings():
    """Plain string emails/phones still work (backward compatibility)."""
    raw_contact = {
        "vcard_id": "compat-1",
        "contact": {
            "fullname": "Plain String",
            "email": "plain@example.com",
            "tel": "+1-555-9999",
        },
    }

    contact = _map_contact(raw_contact)

    assert len(contact.emails) == 1
    assert contact.emails[0].value == "plain@example.com"
    assert contact.emails[0].label is None
    assert contact.emails[0].preferred is False

    assert len(contact.phones) == 1
    assert contact.phones[0].value == "+1-555-9999"


@pytest.mark.unit
def test_contact_mapping_empty_type_list():
    """Dict with empty or missing type list produces no label."""
    raw_contact = {
        "vcard_id": "empty-type-1",
        "contact": {
            "fullname": "No Type",
            "email": {"value": "notype@example.com", "type": []},
        },
    }

    contact = _map_contact(raw_contact)

    assert len(contact.emails) == 1
    assert contact.emails[0].value == "notype@example.com"
    assert contact.emails[0].label is None
    assert contact.emails[0].preferred is False


@pytest.mark.unit
def test_contact_mapping_multiple_dict_emails_with_labels():
    """Multiple dict-format emails preserve individual labels."""
    raw_contact = {
        "vcard_id": "multi-label-1",
        "contact": {
            "fullname": "Multi Label",
            "email": [
                {"value": "home@example.com", "type": ["HOME", "PREF"]},
                {"value": "work@example.com", "type": ["WORK"]},
                {"value": "other@example.com"},
            ],
        },
    }

    contact = _map_contact(raw_contact)

    assert len(contact.emails) == 3
    assert contact.emails[0].value == "home@example.com"
    assert contact.emails[0].label == "home"
    assert contact.emails[0].preferred is True
    assert contact.emails[1].value == "work@example.com"
    assert contact.emails[1].label == "work"
    assert contact.emails[1].preferred is False
    assert contact.emails[2].value == "other@example.com"
    assert contact.emails[2].label is None
    assert contact.primary_email == "home@example.com"


# ============= _event_dict_to_summary tests =============


@pytest.mark.unit
def test_event_dict_to_summary_basic():
    """Test basic mapping with all fields populated."""
    event = {
        "uid": "evt-001",
        "title": "Team Standup",
        "start_datetime": "2025-07-28T09:00:00",
        "end_datetime": "2025-07-28T09:30:00",
        "all_day": False,
        "location": "Room 42",
        "description": "Daily sync",
        "categories": ["work", "meeting"],
        "status": "CONFIRMED",
        "calendar_name": "office",
        "calendar_display_name": "Office Calendar",
    }

    summary = _event_dict_to_summary(event)

    assert summary.uid == "evt-001"
    assert summary.summary == "Team Standup"
    assert summary.start == "2025-07-28T09:00:00"
    assert summary.end == "2025-07-28T09:30:00"
    assert summary.all_day is False
    assert summary.location == "Room 42"
    assert summary.description == "Daily sync"
    assert summary.categories == ["work", "meeting"]
    assert summary.status == "CONFIRMED"
    assert summary.calendar_name == "office"
    assert summary.calendar_display_name == "Office Calendar"


@pytest.mark.unit
def test_event_dict_to_summary_categories_string():
    """Test that comma-separated category string is split into a list."""
    event = {
        "uid": "evt-002",
        "title": "Review",
        "categories": "work, meeting, important",
    }

    summary = _event_dict_to_summary(event)

    assert summary.categories == ["work", "meeting", "important"]


@pytest.mark.unit
def test_event_dict_to_summary_categories_list_passthrough():
    """Test that a list of categories passes through unchanged."""
    event = {
        "uid": "evt-003",
        "title": "Review",
        "categories": ["personal", "health"],
    }

    summary = _event_dict_to_summary(event)

    assert summary.categories == ["personal", "health"]


@pytest.mark.unit
def test_event_dict_to_summary_falsy_location_description():
    """Test that empty/falsy location and description are coerced to None."""
    event = {
        "uid": "evt-004",
        "title": "Quick Chat",
        "location": "",
        "description": "",
    }

    summary = _event_dict_to_summary(event)

    assert summary.location is None
    assert summary.description is None


@pytest.mark.unit
def test_event_dict_to_summary_missing_optional_fields():
    """Test mapping with only required fields present."""
    event = {"uid": "evt-005", "title": "Minimal Event"}

    summary = _event_dict_to_summary(event)

    assert summary.uid == "evt-005"
    assert summary.summary == "Minimal Event"
    assert summary.start == ""
    assert summary.end is None
    assert summary.all_day is False
    assert summary.location is None
    assert summary.description is None
    assert summary.categories == []
    assert summary.status is None
    assert summary.calendar_name is None
    assert summary.calendar_display_name is None


@pytest.mark.unit
def test_event_dict_to_summary_calendar_name_without_display_name():
    """Test single-calendar path: calendar_name set, display_name absent falls back."""
    event = {
        "uid": "evt-006",
        "title": "Personal Errand",
        "calendar_name": "personal",
    }

    summary = _event_dict_to_summary(event)

    assert summary.calendar_name == "personal"
    assert summary.calendar_display_name == "personal"


# ----------------------------------------------------------------------------
# Direct Contact() construction with date-typed birthday — pins #704 / #672
# ----------------------------------------------------------------------------
# These bypass _raw_contact_to_model and hit the Pydantic validator directly,
# so any future code path that constructs Contact from raw vobject output is
# covered without depending on the upstream coercion in server/contacts.py.


@pytest.mark.unit
def test_contact_model_coerces_date_birthday_to_iso():
    """Regression for #704: a datetime.date birthday must coerce to ISO str
    rather than raise a Pydantic ValidationError.
    """
    contact = Contact(uid="c1", fn="Date BDay", birthday=date(2000, 1, 1))
    assert contact.birthday == "2000-01-01"


@pytest.mark.unit
def test_contact_model_coerces_datetime_birthday_to_iso():
    """A datetime.datetime input is also valid in vobject output and must coerce."""
    contact = Contact(
        uid="c2", fn="DateTime BDay", birthday=datetime(2000, 1, 1, 12, 30)
    )
    assert contact.birthday == "2000-01-01T12:30:00"


@pytest.mark.unit
def test_contact_model_string_birthday_passes_through():
    """ISO strings must round-trip unchanged — the validator should be a no-op."""
    contact = Contact(uid="c3", fn="String BDay", birthday="1990-05-15")
    assert contact.birthday == "1990-05-15"


@pytest.mark.unit
def test_contact_model_none_birthday_is_allowed():
    contact = Contact(uid="c4", fn="No BDay")
    assert contact.birthday is None


# ----------------------------------------------------------------------------
# Table model parses without owner_display_name — pins #728
# ----------------------------------------------------------------------------


@pytest.mark.unit
def test_table_parses_without_owner_display_name():
    """Tables app v2.0.1 dropped owner_display_name from the top-level payload.
    Parsing must succeed (#728) — anything else 100%-fails nc_tables_list_tables.
    """
    raw = {
        "id": 1,
        "title": "Welcome to Nextcloud Tables!",
        "ownership": "alice",
        # owner_display_name intentionally absent
    }
    table = Table(**raw)
    assert table.id == 1
    assert table.owner_display_name is None


@pytest.mark.unit
def test_table_parses_with_owner_display_name():
    """When the field is present we still capture it — Optional doesn't drop data."""
    raw = {
        "id": 2,
        "title": "Old API Table",
        "ownership": "bob",
        "owner_display_name": "Bob the Builder",
    }
    table = Table(**raw)
    assert table.owner_display_name == "Bob the Builder"


@pytest.mark.unit
def test_contact_mapping_surfaces_addresses_name_parts_and_extensions():
    """ADR / N / X-* are parsed by pythonvCard4 but were never projected.

    ``Contact.addresses`` was declared and permanently empty, and
    ``given_name``/``family_name`` were never populated, so structured address
    and name data was invisible to every MCP consumer even though the raw
    ``addressdata`` carried it.
    """
    raw_contact = {
        "vcard_id": "adr-1",
        "getetag": '"e"',
        "contact": {
            "fullname": "Alice Doe",
            "n": ["Doe", "Alice", "Q", "Dr", "Jr"],
            "adr": [
                {
                    "value": ["", "", "1 Main St", "Springfield", "IL", "12345", "US"],
                    "type": ["HOME", "PREF"],
                },
                {
                    "value": [
                        "",
                        "Suite 5",
                        "2 Oak Ave",
                        "Shelbyville",
                        "IL",
                        "54321",
                        "US",
                    ],
                    "type": ["WORK"],
                },
            ],
            "custom": {"X-ABLABEL": ["custom"], "X-SOCIALPROFILE": ["https://t.co/x"]},
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.given_name == "Alice"
    assert contact.family_name == "Doe"

    assert len(contact.addresses) == 2
    home = contact.addresses[0]
    assert home.type == "address"
    assert home.components == [
        "",
        "",
        "1 Main St",
        "Springfield",
        "IL",
        "12345",
        "US",
    ]
    assert home.value == ";;1 Main St;Springfield;IL;12345;US"
    assert home.preferred is True
    assert home.label == "home"

    work = contact.addresses[1]
    assert work.preferred is False
    assert work.label == "work"

    assert contact.custom_fields["X-ABLABEL"] == ["custom"]
    assert contact.custom_fields["X-SOCIALPROFILE"] == ["https://t.co/x"]


@pytest.mark.unit
def test_contact_mapping_excludes_photo_from_custom_fields():
    """PHOTO is already surfaced as ``photo``; re-emitting the base64 blob in
    custom_fields would roughly double every list_contacts response."""
    raw_contact = {
        "vcard_id": "photo-1",
        "getetag": '"e"',
        "contact": {
            "fullname": "Alice",
            "photo": "AAAA",
            # The client's _custom_extras drops PHOTO before it reaches here.
            "custom": {"X-KEEP": ["yes"]},
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.photo == "AAAA"
    assert "PHOTO" not in contact.custom_fields
    assert contact.custom_fields["X-KEEP"] == ["yes"]


@pytest.mark.unit
def test_contact_mapping_tolerates_short_and_empty_structured_values():
    """A malformed card can carry fewer N components or an all-empty ADR."""
    raw_contact = {
        "vcard_id": "short-1",
        "getetag": '"e"',
        "contact": {
            "fullname": "Mononym",
            "n": ["OnlyFamily"],
            "adr": [
                {"value": ["", "", "", "", "", "", ""], "type": ["HOME"]},
                "not-a-dict",
            ],
        },
    }

    contact = _map_contact(raw_contact)

    assert contact.family_name == "OnlyFamily"
    assert contact.given_name is None
    # The all-empty ADR and the non-dict entry are both dropped.
    assert contact.addresses == []


@pytest.mark.unit
def test_contact_mapping_handles_missing_structured_keys():
    """Contacts projected before this change (or by a degraded parse) carry no
    ``n``/``adr``/``custom`` keys at all."""
    contact = _map_contact(
        {"vcard_id": "bare-1", "getetag": '"e"', "contact": {"fullname": "Alice"}}
    )

    assert contact.given_name is None
    assert contact.family_name is None
    assert contact.addresses == []
    assert contact.custom_fields == {}


@pytest.mark.unit
def test_contact_mapping_accepts_bare_dict_address():
    """A single ADR arrives as a list from pythonvCard4, but the parser accepts a
    bare dict too — staying symmetric with _parse_vcard_fields, which handles the
    same polymorphism for EMAIL/TEL. Without the normalisation, iterating a dict
    would yield its keys and silently produce zero addresses.
    """
    contact = _map_contact(
        {
            "vcard_id": "adr-dict",
            "getetag": '"e"',
            "contact": {
                "fullname": "Alice",
                "adr": {
                    "value": ["", "", "1 Main St", "Springfield", "", "", "US"],
                    "type": ["HOME"],
                },
            },
        }
    )

    assert len(contact.addresses) == 1
    assert contact.addresses[0].components[2] == "1 Main St"


@pytest.mark.unit
def test_contact_mapping_pads_short_address_to_seven_components():
    """Lenient producers emit fewer than seven ADR parts.

    ``ContactField.components`` documents the seven RFC 6350 §6.3.1 components in
    order, so a short list is padded rather than silently breaking that contract
    for a consumer indexing ``components[6]`` for country.
    """
    contact = _map_contact(
        {
            "vcard_id": "adr-short",
            "getetag": '"e"',
            "contact": {
                "fullname": "Alice",
                "adr": [{"value": ["", "", "1 Main St", "Springfield"], "type": []}],
            },
        }
    )

    assert len(contact.addresses[0].components) == 7
    assert contact.addresses[0].components[2] == "1 Main St"
    assert contact.addresses[0].components[6] == ""
    # The flat display value reflects the padded form, consistent with components.
    assert contact.addresses[0].value == ";;1 Main St;Springfield;;;"


@pytest.mark.unit
def test_contact_mapping_truncates_overlong_address_components():
    """An over-long ADR is truncated to seven so the promised shape holds."""
    contact = _map_contact(
        {
            "vcard_id": "adr-long",
            "getetag": '"e"',
            "contact": {
                "fullname": "Alice",
                "adr": [{"value": [str(i) for i in range(10)], "type": []}],
            },
        }
    )

    assert len(contact.addresses[0].components) == 7
    assert contact.addresses[0].components == ["0", "1", "2", "3", "4", "5", "6"]


@pytest.mark.unit
def test_table_tolerates_missing_ownership():
    """A required `ownership` failed the *entire* nc_tables_list_tables call.

    server/tables.py splats each raw dict straight into Table, so one table
    missing the key costs every table in the response — the same failure mode as
    owner_display_name in #728. Nothing in the codebase reads `ownership`.
    """
    table = Table(id=1, title="Budget")

    assert table.ownership is None
    assert table.owner_display_name is None


@pytest.mark.unit
def test_table_schema_tolerates_missing_columns_and_views():
    """An empty table has no views; a schema response may omit either list."""
    schema = TableSchema(table=Table(id=1, title="Budget"))

    assert schema.columns == []
    assert schema.views == []
