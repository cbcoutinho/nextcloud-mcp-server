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
        assert create_ab_result.isError is False

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
        assert create_c_result.isError is False

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
        assert search_result.isError is False
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
        assert update_result.isError is False
        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        updated = next(c for c in contacts if c["vcard_id"] == contact_uid)
        updated_vcard = updated.get("addressdata", "")
        assert "mcp-test.example.com" in updated_vcard
        # Prior properties must not have been clobbered by the merge.
        assert "ORG:MCP Test Corp" in updated_vcard

        # 5. Delete contact via MCP
        logger.info("Deleting contact %s via MCP", contact_uid)
        delete_c_result = await nc_mcp_client.call_tool(
            "nc_contacts_delete_contact",
            {"addressbook": addressbook_name, "uid": contact_uid},
        )
        assert delete_c_result.isError is False

        # 6. Verify contact deletion
        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        assert not any(c["vcard_id"] == contact_uid for c in contacts)

        # 7. Delete address book via MCP
        logger.info("Deleting address book %s via MCP", addressbook_name)
        delete_ab_result = await nc_mcp_client.call_tool(
            "nc_contacts_delete_addressbook", {"name": addressbook_name}
        )
        assert delete_ab_result.isError is False

        # 8. Verify address book deletion
        addressbooks = await nc_client.contacts.list_addressbooks()
        assert not any(ab["name"] == addressbook_name for ab in addressbooks)

    finally:
        # Cleanup in case of failure
        try:
            await nc_client.contacts.delete_addressbook(name=addressbook_name)
        except Exception:
            pass
