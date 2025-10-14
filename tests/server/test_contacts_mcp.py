"""Integration tests for Contacts MCP tools."""

import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


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
    }

    try:
        # 1. Create address book via MCP
        logger.info(f"Creating address book via MCP: {addressbook_name}")
        create_ab_result = await nc_mcp_client.call_tool(
            "nc_contacts_create_addressbook",
            {"name": addressbook_name, "display_name": f"MCP Test {addressbook_name}"},
        )
        assert create_ab_result.isError is False

        # 2. Verify address book creation
        addressbooks = await nc_client.contacts.list_addressbooks()
        assert any(ab["name"] == addressbook_name for ab in addressbooks)

        # 3. Create contact via MCP
        logger.info(f"Creating contact in {addressbook_name} via MCP")
        create_c_result = await nc_mcp_client.call_tool(
            "nc_contacts_create_contact",
            {
                "addressbook": addressbook_name,
                "uid": contact_uid,
                "contact_data": contact_data,
            },
        )
        assert create_c_result.isError is False

        # 4. Verify contact creation
        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        assert any(c["vcard_id"] == contact_uid for c in contacts)

        # 5. Delete contact via MCP
        logger.info(f"Deleting contact {contact_uid} via MCP")
        delete_c_result = await nc_mcp_client.call_tool(
            "nc_contacts_delete_contact",
            {"addressbook": addressbook_name, "uid": contact_uid},
        )
        assert delete_c_result.isError is False

        # 6. Verify contact deletion
        contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
        assert not any(c["vcard_id"] == contact_uid for c in contacts)

        # 7. Delete address book via MCP
        logger.info(f"Deleting address book {addressbook_name} via MCP")
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
