"""Unit tests for the contacts client vCard builder.

These exercise ``_build_contact_from_data`` in isolation — no HTTP, no fixtures —
so they cover the issue #716 regression surface and the edge cases flagged in
PR #719 review without standing up the compose stack.
"""

from datetime import date

import pytest

from nextcloud_mcp_server.client.contacts import (
    _build_contact_from_data,
    _normalize_contact_data,
)

pytestmark = pytest.mark.unit


def _vcard(**kwargs) -> str:
    """Build a vCard from ``contact_data`` with a fixed uid, return the serialised text."""
    return _build_contact_from_data(kwargs, uid="unit-test-uid").to_vcard()


def test_issue_716_minimal_payload_keeps_all_fields():
    """Reporter's exact payload from issue #716: every field must survive."""
    vcard = _vcard(
        fn="Repro User",
        email="repro@example.com",
        phone="555-0716",
        organization="Acme Corp",
        note="Issue 716",
    )
    assert "FN:Repro User" in vcard
    assert "EMAIL" in vcard and "repro@example.com" in vcard
    assert "TEL" in vcard and "555-0716" in vcard
    assert "ORG:Acme Corp" in vcard
    assert "NOTE:Issue 716" in vcard


def test_org_preserves_comma_in_company_name():
    """Regression: ``_as_list`` used to comma-split ORG, mangling names like
    "Smith, Jones & Associates" into a two-component ORG. After the fix the whole
    string is a single ORG component (with the comma RFC-6350-escaped as ``\\,``).
    """
    vcard = _vcard(fn="Alice", organization="Smith, Jones & Associates")
    org_line = next(line for line in vcard.splitlines() if line.startswith("ORG"))
    # Single component: no unescaped semicolon separator.
    payload = org_line.split(":", 1)[1]
    assert ";" not in payload
    # Comma is escaped per RFC 6350 but the logical value is preserved.
    assert payload.replace(r"\,", ",") == "Smith, Jones & Associates"


def test_org_list_input_produces_structured_org():
    """A list input is the opt-in shape for multi-component ORG (Company;Department)."""
    vcard = _vcard(fn="Alice", org=["Acme", "Engineering"])
    assert "ORG:Acme;Engineering" in vcard


def test_invalid_bday_is_dropped_not_raised(caplog):
    """An unparseable BDAY must warn and be omitted, not crash the call."""
    import logging

    with caplog.at_level(
        logging.WARNING, logger="nextcloud_mcp_server.client.contacts"
    ):
        vcard = _vcard(fn="Alice", bday="not-a-date")
    assert "BDAY" not in vcard
    assert any("bday" in r.message.lower() for r in caplog.records)


def test_valid_iso_bday_is_persisted():
    vcard = _vcard(fn="Alice", bday="1990-05-01")
    assert "BDAY:1990-05-01" in vcard


def test_date_object_bday_is_persisted():
    vcard = _vcard(fn="Alice", bday=date(1985, 12, 24))
    assert "BDAY:1985-12-24" in vcard


def test_tel_takes_precedence_over_phone_alias():
    """When the caller supplies both canonical and alias, canonical wins. Documents
    the precedence so future callers aren't surprised.
    """
    vcard = _vcard(fn="Alice", tel="111-1111", phone="222-2222")
    assert "111-1111" in vcard
    assert "222-2222" not in vcard


def test_organization_alias_fills_in_when_org_absent():
    vcard = _vcard(fn="Alice", organization="Acme")
    assert "ORG:Acme" in vcard


def test_categories_string_is_split_on_commas():
    vcard = _vcard(fn="Alice", categories="friends,work,vip")
    cat_line = next(
        line for line in vcard.splitlines() if line.startswith("CATEGORIES")
    )
    assert cat_line == "CATEGORIES:friends,work,vip"


def test_categories_list_passes_through_unchanged():
    """A caller that already supplied a list shouldn't have their entries split again
    — ``["friends,work"]`` stays as one item (with the comma RFC-6350-escaped), not
    two categories ``friends`` + ``work``.
    """
    vcard = _vcard(fn="Alice", categories=["friends,work"])
    cat_line = next(
        line for line in vcard.splitlines() if line.startswith("CATEGORIES")
    )
    payload = cat_line.split(":", 1)[1]
    assert payload == r"friends\,work"  # one item, comma escaped


def test_nickname_bare_string_is_not_char_iterated():
    """Regression: pythonvCard4 iterates bare strings; we wrap to prevent that."""
    vcard = _vcard(fn="Alice", nickname="Bob")
    nick_line = next(line for line in vcard.splitlines() if line.startswith("NICKNAME"))
    assert nick_line == "NICKNAME:Bob"


def test_url_bare_string_is_not_char_iterated():
    vcard = _vcard(fn="Alice", url="https://example.com")
    # Must appear as a single URL, not one URL: per character.
    url_lines = [line for line in vcard.splitlines() if line.startswith("URL")]
    assert url_lines == ["URL:https://example.com"]


def test_unknown_keys_are_ignored_without_error(caplog):
    """Future-compat: callers sending unknown keys shouldn't blow up."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="nextcloud_mcp_server.client.contacts"):
        vcard = _vcard(fn="Alice", totally_made_up_field="ignored")
    assert "FN:Alice" in vcard
    assert "totally_made_up_field" not in vcard
    # A debug log is expected but not required — main guarantee is that no exception is raised.


def test_empty_email_is_skipped():
    """An empty string for email must not emit an EMAIL: line."""
    vcard = _vcard(fn="Alice", email="")
    assert "EMAIL" not in vcard


def test_dict_form_email_preserves_custom_type():
    vcard = _vcard(
        fn="Alice",
        email={"value": "work@example.com", "type": ["WORK"]},
    )
    assert "EMAIL;TYPE=WORK:work@example.com" in vcard


class TestNormalizeContactData:
    """Direct tests for the alias helper — it's load-bearing for update_contact too."""

    def test_phone_maps_to_tel(self):
        assert _normalize_contact_data({"phone": "123"}) == {"tel": "123"}

    def test_organization_maps_to_org(self):
        assert _normalize_contact_data({"organization": "Acme"}) == {"org": "Acme"}

    def test_canonical_wins_when_both_present(self):
        """Caller intent: they set ``tel`` deliberately. A stray ``phone`` entry
        must not clobber the canonical value.
        """
        out = _normalize_contact_data({"tel": "canonical", "phone": "alias"})
        assert out == {"tel": "canonical"}

    def test_does_not_mutate_input(self):
        original = {"phone": "123", "organization": "Acme"}
        _normalize_contact_data(original)
        assert original == {"phone": "123", "organization": "Acme"}

    def test_passthrough_for_unknown_keys(self):
        assert _normalize_contact_data({"foo": "bar"}) == {"foo": "bar"}


class TestMergeVcardProperties:
    """Direct tests for ``_merge_vcard_properties`` — the primary update path.

    Written in response to PR #719 review claiming NICKNAME/BDAY/CATEGORIES are not
    updatable via this function. These tests pin the actual behaviour so future
    regressions (or claims) can be answered in one line.
    """

    @staticmethod
    def _merge(raw: str, data: dict) -> str:
        from nextcloud_mcp_server.client.contacts import ContactsClient

        client = ContactsClient.__new__(ContactsClient)  # no HTTP / no __init__
        return client._merge_vcard_properties(raw, data, uid="merge-test")

    def test_nickname_overwrites_existing_line(self):
        """Existing NICKNAME must be replaced with the new value, not preserved."""
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nNICKNAME:Bob\nEND:VCARD\n"
        result = self._merge(existing, {"nickname": "Robert"})
        assert "NICKNAME:Robert" in result
        assert "NICKNAME:Bob" not in result

    def test_bday_overwrites_existing_line(self):
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nBDAY:1990-05-01\nEND:VCARD\n"
        result = self._merge(existing, {"bday": "1991-06-02"})
        assert "BDAY:1991-06-02" in result
        assert "BDAY:1990-05-01" not in result

    def test_categories_overwrites_existing_line(self):
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nCATEGORIES:old,stale\nEND:VCARD\n"
        result = self._merge(existing, {"categories": ["vip", "new"]})
        assert "CATEGORIES:vip,new" in result
        assert "old,stale" not in result

    def test_nickname_added_when_not_in_existing_vcard(self):
        """If the existing vCard has no NICKNAME line, update must append one."""
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        result = self._merge(existing, {"nickname": "Bob"})
        assert "NICKNAME:Bob" in result

    def test_bday_added_when_not_in_existing_vcard(self):
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        result = self._merge(existing, {"bday": "1990-05-01"})
        assert "BDAY:1990-05-01" in result

    def test_categories_added_when_not_in_existing_vcard(self):
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        result = self._merge(existing, {"categories": "a,b,c"})
        assert "CATEGORIES:a,b,c" in result

    def test_url_update_preserves_unrelated_properties(self):
        """A URL update must not clobber ORG / NOTE / TEL from the existing vCard."""
        existing = (
            "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\n"
            "ORG:Acme\nTEL:555-1234\nNOTE:keep me\nEND:VCARD\n"
        )
        result = self._merge(existing, {"url": "https://example.com"})
        assert "URL:https://example.com" in result
        assert "ORG:Acme" in result
        assert "TEL:555-1234" in result
        assert "NOTE:keep me" in result
