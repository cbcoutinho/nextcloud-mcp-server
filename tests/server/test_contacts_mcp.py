"""Integration tests for Contacts MCP tools."""

import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


def _extract_payload(tool_result) -> dict:
    """Return the JSON-decoded text content of an MCP tool result."""
    return json.loads(tool_result.content[0].text)


async def test_mcp_contacts_workflow(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test complete Contacts workflow via MCP tools with verification via NextcloudClient."""

    addressbook_name = f"mcp-test-addressbook-{uuid.uuid4().hex[:8]}"
    unique_suffix = uuid.uuid4().hex[:8]
    contact_uid = f"mcp-contact-{unique_suffix}"
    contact_data = {
        "fn": f"MCP Contact {unique_suffix}",
        "email": f"mcp.contact.{unique_suffix}@example.com",
        "tel": "1234567890",
        # Regression for issue #716 — these were silently dropped before
        "organization": "MCP Test Corp",
        "note": f"Created by test {unique_suffix}",
    }

    try:
        # 1. Create address book via MCP
        logger.info("Creating address book via MCP: %s", addressbook_name)
        create_ab_result = await nc_mcp_client.call_tool(
            "nc_contacts_create_addressbook",
            {"name": addressbook_name, "display_name": f"MCP Test {addressbook_name}"},
        )
        assert create_ab_result.is_error is False

        # 2. Verify address book creation
        addressbooks = await nc_client.contacts.list_addressbooks()
        assert any(ab["name"] == addressbook_name for ab in addressbooks)

        # 3. Create contact via MCP
        logger.info("Creating contact in %s via MCP", addressbook_name)
        create_c_result = await nc_mcp_client.call_tool(
            "nc_contacts_create_contact",
            {
                "addressbook": addressbook_name,
                "uid": contact_uid,
                "contact_data": contact_data,
            },
        )
        assert create_c_result.is_error is False

        # 4. Verify contact creation (and that all fields — #716 — actually persisted)
        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        created = next((c for c in contacts if c["vcard_id"] == contact_uid), None)
        assert created is not None
        raw_vcard = created.get("addressdata", "")
        assert "ORG:MCP Test Corp" in raw_vcard
        assert f"NOTE:Created by test {unique_suffix}" in raw_vcard

        # 4a. Read-side round-trip — issue #716 follow-up. The write side has
        # been correct since PR #719, but the MCP list/search tools returned
        # ``organization: null`` / ``note: null`` because pythonvCard4 stashes
        # ORG/TITLE in ``custom`` and the server's _raw_contact_to_model never
        # surfaced ``note`` / ``urls`` either.
        search_result = await nc_mcp_client.call_tool(
            "nc_contacts_search_contacts",
            {"query": unique_suffix, "addressbook": addressbook_name},
        )
        assert search_result.is_error is False
        search_payload = _extract_payload(search_result)
        assert search_payload["total_count"] == 1
        searched = search_payload["contacts"][0]
        assert searched["uid"] == contact_uid
        assert searched["organization"] == "MCP Test Corp"
        assert searched["note"] == f"Created by test {unique_suffix}"

        # 4b. Update with a URL — regression guard for PR #719 review:
        # _merge_vcard_properties previously had no URL handler, silently dropping it.
        update_result = await nc_mcp_client.call_tool(
            "nc_contacts_update_contact",
            {
                "addressbook": addressbook_name,
                "uid": contact_uid,
                "contact_data": {"url": "https://mcp-test.example.com"},
            },
        )
        assert update_result.is_error is False
        # The tool now returns a typed UpdateContactResponse carrying the new etag.
        update_payload = _extract_payload(update_result)
        assert update_payload["success"] is True
        assert update_payload["addressbook"] == addressbook_name
        assert update_payload["contact"]["uid"] == contact_uid
        chained_etag = update_payload["contact"]["etag"]
        assert chained_etag

        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        updated = next(c for c in contacts if c["vcard_id"] == contact_uid)
        updated_vcard = updated.get("addressdata", "")
        assert "mcp-test.example.com" in updated_vcard
        # Prior properties must not have been clobbered by the merge.
        assert "ORG:MCP Test Corp" in updated_vcard

        # 4c. Chain a second update using the etag the previous one returned —
        # no intervening read. Supplying an etag used to skip the existing-vCard
        # fetch entirely and rebuild the card from the supplied keys, destroying
        # every property not passed in. Guard that the merge still runs.
        chained_result = await nc_mcp_client.call_tool(
            "nc_contacts_update_contact",
            {
                "addressbook": addressbook_name,
                "uid": contact_uid,
                "contact_data": {"note": "Updated with etag"},
                "etag": chained_etag,
            },
        )
        assert chained_result.is_error is False
        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        chained_vcard = next(c for c in contacts if c["vcard_id"] == contact_uid).get(
            "addressdata", ""
        )
        assert "NOTE:Updated with etag" in chained_vcard
        # Properties absent from contact_data must survive the etag path.
        assert "ORG:MCP Test Corp" in chained_vcard
        assert "mcp-test.example.com" in chained_vcard

        # 4d. A stale etag must be rejected, not silently applied.
        stale_result = await nc_mcp_client.call_tool(
            "nc_contacts_update_contact",
            {
                "addressbook": addressbook_name,
                "uid": contact_uid,
                "contact_data": {"note": "Should not land"},
                "etag": chained_etag,
            },
        )
        assert stale_result.is_error is True

        # 5. Delete contact via MCP
        logger.info("Deleting contact %s via MCP", contact_uid)
        delete_c_result = await nc_mcp_client.call_tool(
            "nc_contacts_delete_contact",
            {"addressbook": addressbook_name, "uid": contact_uid},
        )
        assert delete_c_result.is_error is False

        # 6. Verify contact deletion
        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        assert not any(c["vcard_id"] == contact_uid for c in contacts)

        # 7. Delete address book via MCP
        logger.info("Deleting address book %s via MCP", addressbook_name)
        delete_ab_result = await nc_mcp_client.call_tool(
            "nc_contacts_delete_addressbook", {"name": addressbook_name}
        )
        assert delete_ab_result.is_error is False

        # 8. Verify address book deletion
        addressbooks = await nc_client.contacts.list_addressbooks()
        assert not any(ab["name"] == addressbook_name for ab in addressbooks)

    finally:
        # Cleanup in case of failure
        try:
            await nc_client.contacts.delete_addressbook(name=addressbook_name)
        except Exception:
            pass


async def test_mcp_contacts_surfaces_structured_fields_and_survives_bad_cards(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """ADR / N / X-* reach the model, and one unparseable card can't kill the list.

    Two contacts are injected by raw CardDAV PUT because ``create_contact`` has no
    vocabulary for ADR or X-* properties:

    * ``rich`` carries ADR, N and X-* — previously parsed by pythonvCard4 but
      never projected, so ``addresses`` was declared and permanently empty.
    * ``geo`` carries a vCard 3.0 ``GEO:lat,lon`` (RFC 2426 uses a comma;
      pythonvCard4 splits on ";" and raises). Unguarded, this one contact made the
      whole addressbook unlistable.
    """
    addressbook_name = f"mcp-struct-{uuid.uuid4().hex[:8]}"
    suffix = uuid.uuid4().hex[:8]
    rich_uid = f"rich-{suffix}"
    geo_uid = f"geo-{suffix}"

    base = f"/remote.php/dav/addressbooks/users/{nc_client.contacts.username}/{addressbook_name}"

    rich_vcard = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"UID:{rich_uid}\r\n"
        "FN:Alice Doe\r\n"
        "N:Doe;Alice;Q;Dr;Jr\r\n"
        "ADR;TYPE=HOME:;;1 Main St;Springfield;IL;12345;US\r\n"
        "X-ABLabel:custom-label\r\n"
        "END:VCARD\r\n"
    )
    # GEO in vCard 3.0 comma form — the shape that used to break the listing.
    geo_vcard = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"UID:{geo_uid}\r\n"
        "FN:Geo Person\r\n"
        "GEO:37.386013,-122.082932\r\n"
        "END:VCARD\r\n"
    )

    try:
        await nc_client.contacts.create_addressbook(
            name=addressbook_name, display_name=f"Struct {addressbook_name}"
        )
        for uid_, vcard in ((rich_uid, rich_vcard), (geo_uid, geo_vcard)):
            await nc_client.contacts._make_request(
                "PUT",
                f"{base}/{uid_}.vcf",
                content=vcard,
                headers={"Content-Type": "text/vcard; charset=utf-8"},
            )

        list_result = await nc_mcp_client.call_tool(
            "nc_contacts_list_contacts", {"addressbook": addressbook_name}
        )
        assert list_result.is_error is False
        payload = _extract_payload(list_result)

        by_uid = {c["uid"]: c for c in payload["contacts"]}
        # Both contacts come back — the unparseable one no longer takes the
        # listing down with it.
        assert rich_uid in by_uid, "structured contact missing from listing"
        assert geo_uid in by_uid, "unparseable contact took down the whole listing"

        rich = by_uid[rich_uid]
        assert rich["given_name"] == "Alice"
        assert rich["family_name"] == "Doe"
        assert len(rich["addresses"]) == 1
        address = rich["addresses"][0]
        assert address["type"] == "address"
        assert address["components"] == [
            "",
            "",
            "1 Main St",
            "Springfield",
            "IL",
            "12345",
            "US",
        ]
        assert address["label"] == "home"
        assert rich["custom_fields"]["X-ABLABEL"] == ["custom-label"]

    finally:
        try:
            await nc_client.contacts.delete_addressbook(name=addressbook_name)
        except Exception:
            pass
