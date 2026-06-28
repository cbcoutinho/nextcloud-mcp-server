"""Unit tests for the contacts client vCard builder.

These exercise ``_build_contact_from_data`` in isolation — no HTTP, no fixtures —
so they cover the issue #716 regression surface and the edge cases flagged in
PR #719 review without standing up the compose stack.
"""

from datetime import date

import pytest

from nextcloud_mcp_server.client.contacts import (
    ContactsClient,
    _build_contact_from_data,
    _first_custom,
    _normalize_contact_data,
    _wrap_contact_field,
)

pytestmark = pytest.mark.unit


def _vcard(**kwargs) -> str:
    """Build a vCard from ``contact_data`` with a fixed uid, return the serialised text.

    Mirrors ``create_contact``'s real call chain: normalise aliases first, then hand
    canonical keys to ``_build_contact_from_data``.
    """
    data = _normalize_contact_data(kwargs)
    return _build_contact_from_data(data, uid="unit-test-uid").to_vcard()


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


def test_dict_form_email_with_bare_string_type():
    """Regression: a dict with ``type="WORK"`` (bare string) used to be
    char-iterated into ``["W","O","R","K"]`` because of an unguarded ``list()``
    call inside ``_wrap_contact_field``.
    """
    vcard = _vcard(
        fn="Alice",
        email={"value": "work@example.com", "type": "WORK"},
    )
    assert "EMAIL;TYPE=WORK:work@example.com" in vcard
    # The bug would emit something like ``EMAIL;TYPE=W,O,R,K:`` — guard against it.
    assert "TYPE=W," not in vcard


def test_wrap_field_dict_without_value_is_dropped():
    """Dict inputs lacking the ``value`` key are silently dropped so malformed
    payloads don't emit an EMAIL/TEL line pointing at nothing.
    """
    assert _wrap_contact_field({"type": ["WORK"]}) == []
    # Mixed list: the valid entry survives, the value-less dict is omitted.
    out = _wrap_contact_field(
        [{"value": "ok@example.com", "type": ["WORK"]}, {"type": ["HOME"]}]
    )
    assert out == [{"value": "ok@example.com", "type": ["WORK"]}]


def test_missing_fn_logs_warning(caplog):
    """A missing ``fn`` should log a warning so operators notice malformed payloads."""
    import logging

    with caplog.at_level(
        logging.WARNING, logger="nextcloud_mcp_server.client.contacts"
    ):
        try:
            _build_contact_from_data({"email": "x@example.com"}, uid="no-fn-uid")
        except Exception:
            # pythonvCard4 may raise on missing fn; we only care about the warning log.
            pass
    assert any("fn" in r.message.lower() for r in caplog.records)


def _multistatus(*object_names: str, addressbook: str = "contacts") -> bytes:
    """Build a minimal PROPFIND multistatus body for ``_list_object_names``.

    Includes the collection itself (trailing-slash href) plus one ``response``
    per object name so the parser's collection-skipping is exercised.
    """
    base = f"/remote.php/dav/addressbooks/users/testuser/{addressbook}"
    responses = [
        f"<d:response><d:href>{base}/</d:href>"
        "<d:propstat><d:prop><d:getetag>&quot;col&quot;</d:getetag></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    ]
    for name in object_names:
        responses.append(
            f"<d:response><d:href>{base}/{name}</d:href>"
            "<d:propstat><d:prop><d:getetag>&quot;e&quot;</d:getetag></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    body = (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:">' + "".join(responses) + "</d:multistatus>"
    )
    return body.encode()


class TestObjectNameResolution:
    """Issue #874: the CardDAV object filename is independent of the vCard UID
    and may lack a ``.vcf`` extension, so write paths must resolve the real
    object name instead of assuming ``<uid>.vcf``.
    """

    @staticmethod
    def _client(mocker, multistatus: bytes) -> ContactsClient:
        client = ContactsClient.__new__(ContactsClient)  # no HTTP / no __init__
        client.username = "testuser"
        response = mocker.Mock()
        response.content = multistatus
        client._make_request = mocker.AsyncMock(return_value=response)
        return client

    async def test_list_object_names_skips_collection(self, mocker):
        client = self._client(mocker, _multistatus("alice.vcf", "default"))
        names = await client._list_object_names("contacts")
        assert names == ["alice.vcf", "default"]  # collection href omitted

    async def test_resolves_conventional_vcf_name(self, mocker):
        client = self._client(mocker, _multistatus("alice.vcf"))
        assert await client._resolve_object_name("contacts", "alice") == "alice.vcf"

    async def test_resolves_name_without_vcf_extension(self, mocker):
        """The #874 case: object stored at ``.../default`` (no extension)."""
        client = self._client(mocker, _multistatus("default"))
        assert await client._resolve_object_name("contacts", "default") == "default"

    async def test_prefers_vcf_when_both_present(self, mocker):
        """Deterministic tie-break: ``<uid>.vcf`` wins over a bare ``<uid>``."""
        client = self._client(mocker, _multistatus("dup", "dup.vcf"))
        assert await client._resolve_object_name("contacts", "dup") == "dup.vcf"

    async def test_returns_none_when_no_match(self, mocker):
        client = self._client(mocker, _multistatus("alice.vcf"))
        assert await client._resolve_object_name("contacts", "missing") is None

    async def test_delete_targets_real_no_extension_path(self, mocker):
        """Regression for #874: delete must hit ``.../default`` not ``.../default.vcf``."""
        client = ContactsClient.__new__(ContactsClient)
        client.username = "testuser"
        client._principal_discovered = True
        mocker.patch.object(
            client, "_resolve_object_name", mocker.AsyncMock(return_value="default")
        )
        make_request = mocker.patch.object(client, "_make_request", mocker.AsyncMock())
        await client.delete_contact(addressbook="contacts", uid="default")
        make_request.assert_awaited_once_with(
            "DELETE",
            "/remote.php/dav/addressbooks/users/testuser/contacts/default",
        )

    async def test_delete_falls_back_to_vcf_when_unresolved(self, mocker):
        """A genuinely missing contact resolves to None → fall back to the
        conventional name so the caller still gets a clean 404 from the DELETE.
        """
        client = ContactsClient.__new__(ContactsClient)
        client.username = "testuser"
        client._principal_discovered = True
        mocker.patch.object(
            client, "_resolve_object_name", mocker.AsyncMock(return_value=None)
        )
        make_request = mocker.patch.object(client, "_make_request", mocker.AsyncMock())
        await client.delete_contact(addressbook="contacts", uid="ghost")
        make_request.assert_awaited_once_with(
            "DELETE",
            "/remote.php/dav/addressbooks/users/testuser/contacts/ghost.vcf",
        )

    async def test_update_targets_real_no_extension_path(self, mocker):
        """Regression for #874: update must PUT to ``.../default`` not
        ``.../default.vcf`` (parallels the delete coverage).
        """
        client = ContactsClient.__new__(ContactsClient)
        client.username = "testuser"
        client._principal_discovered = True
        mocker.patch.object(
            client, "_resolve_object_name", mocker.AsyncMock(return_value="default")
        )
        mocker.patch.object(
            client,
            "_fetch_raw_vcard",
            mocker.AsyncMock(
                return_value=(
                    "BEGIN:VCARD\nVERSION:3.0\nUID:default\nFN:No Ext\nEND:VCARD\n",
                    '"etag"',
                )
            ),
        )
        make_request = mocker.patch.object(client, "_make_request", mocker.AsyncMock())
        await client.update_contact(
            addressbook="contacts", uid="default", contact_data={"fn": "Updated"}
        )
        method, url = make_request.await_args.args[0], make_request.await_args.args[1]
        assert method == "PUT"
        assert url == "/remote.php/dav/addressbooks/users/testuser/contacts/default"

    async def test_update_falls_back_to_vcf_when_unresolved(self, mocker):
        """When resolution finds nothing, update falls back to ``<uid>.vcf`` so
        the caller still gets a clean 404 from the PUT (mirrors delete).
        """
        client = ContactsClient.__new__(ContactsClient)
        client.username = "testuser"
        client._principal_discovered = True
        mocker.patch.object(
            client, "_resolve_object_name", mocker.AsyncMock(return_value=None)
        )
        make_request = mocker.patch.object(client, "_make_request", mocker.AsyncMock())
        # Supplying an etag skips the existing-vCard fetch; update builds a fresh
        # vCard and PUTs it to the fallback path.
        await client.update_contact(
            addressbook="contacts",
            uid="ghost",
            contact_data={"fn": "Ghost"},
            etag='"x"',
        )
        method, url = make_request.await_args.args[0], make_request.await_args.args[1]
        assert method == "PUT"
        assert url == "/remote.php/dav/addressbooks/users/testuser/contacts/ghost.vcf"


class TestFirstCustom:
    """``_first_custom`` is the read-side companion to PR #719 — it pulls
    ORG / TITLE / unencoded PHOTO out of pythonvCard4's ``custom`` dict because
    the library has no typed parser for them.
    """

    def test_returns_first_value_from_list(self):
        assert _first_custom({"ORG": ["Acme Corp"]}, "ORG") == "Acme Corp"

    def test_returns_first_value_when_library_uses_bare_string(self):
        """The library's typeshed allows ``str`` as a value shape too. Accept it
        so we don't break if the parser changes shape upstream.
        """
        assert _first_custom({"TITLE": "Engineer"}, "TITLE") == "Engineer"

    def test_returns_none_for_missing_key(self):
        assert _first_custom({"ORG": ["Acme"]}, "TITLE") is None

    def test_returns_none_for_empty_list(self):
        assert _first_custom({"ORG": []}, "ORG") is None

    def test_returns_none_for_empty_string(self):
        assert _first_custom({"ORG": ""}, "ORG") is None


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

    def test_dict_email_input_preserves_existing_line(self):
        """Regression: a dict-form email on update used to consume the existing
        EMAIL: line and write nothing, silently deleting the contact's email.
        Now the original line is preserved when the input shape isn't a plain str.
        """
        existing = (
            "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\n"
            "EMAIL;TYPE=HOME:alice@example.com\nEND:VCARD\n"
        )
        result = self._merge(
            existing, {"email": {"value": "work@example.com", "type": ["WORK"]}}
        )
        assert "EMAIL;TYPE=HOME:alice@example.com" in result

    def test_list_tel_input_preserves_existing_line(self):
        """Same regression as the email branch but for TEL — a list-shaped tel
        input must not silently drop the existing phone number.
        """
        existing = (
            "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\n"
            "TEL;TYPE=HOME:555-0001\nEND:VCARD\n"
        )
        result = self._merge(
            existing, {"tel": [{"value": "555-9999", "type": ["WORK"]}]}
        )
        assert "TEL;TYPE=HOME:555-0001" in result

    def test_invalid_bday_on_update_preserves_existing_line(self):
        """A non-ISO BDAY string must not produce a malformed vCard line on update.
        We share validation with the create path; invalid → keep the existing line.
        """
        existing = (
            "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\n"
            "BDAY:1990-05-01\nEND:VCARD\n"
        )
        result = self._merge(existing, {"bday": "not-a-date"})
        assert "BDAY:1990-05-01" in result
        assert "BDAY:not-a-date" not in result

    def test_invalid_bday_on_add_new_is_dropped(self):
        """No existing BDAY + invalid input → no BDAY line appended (vs. raw write)."""
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        result = self._merge(existing, {"bday": "not-a-date"})
        assert "BDAY" not in result

    def test_newline_in_note_does_not_inject_property(self):
        """Regression: a literal newline in a value must not terminate the line
        and inject a fresh vCard property.
        """
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        result = self._merge(
            existing, {"note": "harmless\nEMAIL:attacker@evil.example"}
        )
        # The injected property must not appear as a real EMAIL line.
        lines = result.splitlines()
        assert "EMAIL:attacker@evil.example" not in lines
        # The note value is preserved with newlines escaped per RFC 6350.
        assert any(line.startswith("NOTE:") and "\\n" in line for line in lines)

    def test_list_org_overwrites_with_semicolon_join(self):
        """Regression: list-form ORG used to fall through ``_safe_vcard_value``
        unchanged and emit a Python ``repr`` like ``ORG:['Acme', 'Engineering']``.
        Per RFC 6350 §6.6.4 components are ``;``-separated.
        """
        existing = (
            "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nORG:OldCo\nEND:VCARD\n"
        )
        result = self._merge(existing, {"org": ["Acme", "Engineering"]})
        assert "ORG:Acme;Engineering" in result
        assert "ORG:OldCo" not in result
        assert "[" not in result and "'Acme'" not in result

    def test_list_org_added_with_semicolon_join(self):
        """Add-new branch: list ORG without an existing line still serialises
        with ``;`` rather than as a Python list repr.
        """
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        result = self._merge(existing, {"org": ["Acme", "Engineering"]})
        assert "ORG:Acme;Engineering" in result
        assert "[" not in result

    def test_dict_email_on_no_existing_line_warns(self, caplog):
        """No existing EMAIL + dict input is a known limitation of the text-merge
        path. Surface it as a warning so the silent no-op is at least observable.
        """
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        with caplog.at_level("WARNING"):
            result = self._merge(
                existing, {"email": {"value": "alice@work.com", "type": ["WORK"]}}
            )
        # The dict input is not applied; no EMAIL line is added.
        assert "EMAIL" not in result
        # A warning specifically calls out the dict/list shape and recommends
        # plain str / create_contact as alternatives.
        assert any(
            "email" in r.message and "dict/list shape" in r.message
            for r in caplog.records
        )

    def test_list_tel_on_no_existing_line_warns(self, caplog):
        """Same warning behaviour for TEL — list input on a contact without an
        existing TEL line must not silently disappear.
        """
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        with caplog.at_level("WARNING"):
            result = self._merge(
                existing, {"tel": [{"value": "555-9999", "type": ["WORK"]}]}
            )
        assert "TEL" not in result
        assert any(
            "tel" in r.message and "dict/list shape" in r.message
            for r in caplog.records
        )

    def test_str_email_does_not_warn(self, caplog):
        """Plain string email is the supported shape — no warning should fire."""
        existing = "BEGIN:VCARD\nVERSION:3.0\nUID:merge-test\nFN:Alice\nEND:VCARD\n"
        with caplog.at_level("WARNING"):
            result = self._merge(existing, {"email": "alice@work.com"})
        assert "EMAIL:alice@work.com" in result
        assert not any("dict/list shape" in r.message for r in caplog.records)
