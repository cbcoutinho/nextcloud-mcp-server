"""Integration tests for Contacts CardDAV operations."""

import logging
import uuid

import pytest

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


async def test_list_addressbooks(nc_client: NextcloudClient):
    """Test listing available addressbooks."""
    addressbooks = await nc_client.contacts.list_addressbooks()

    assert isinstance(addressbooks, list)

    if not addressbooks:
        pytest.skip("No addressbooks available - Contacts app may not be enabled")

    logger.info(f"Found {len(addressbooks)} addressbooks")

    # Check structure of addressbooks
    for addressbook in addressbooks:
        assert "name" in addressbook
        assert "display_name" in addressbook
        assert "getctag" in addressbook

        logger.info(
            f"Addressbook: {addressbook['name']} - {addressbook['display_name']}"
        )


async def test_create_and_delete_addressbook(
    nc_client: NextcloudClient, temporary_addressbook: str
):
    """Test creating and deleting a basic addressbook."""
    addressbooks = await nc_client.contacts.list_addressbooks()
    addressbook_names = [ab["name"] for ab in addressbooks]
    assert temporary_addressbook in addressbook_names


async def test_list_contacts(
    nc_client: NextcloudClient, temporary_addressbook: str, temporary_contact: str
):
    """Test listing contacts in an addressbook."""
    contacts = await nc_client.contacts.list_contacts(addressbook=temporary_addressbook)
    contact_uids = [c["vcard_id"] for c in contacts]
    assert temporary_contact in contact_uids


async def test_full_contact_workflow(
    nc_client: NextcloudClient, temporary_addressbook: str
):
    """Test the full workflow of creating, retrieving, and deleting a contact."""
    addressbook_name = temporary_addressbook
    contact_uid = f"test-contact-{uuid.uuid4().hex[:8]}"
    contact_data = {
        "fn": "Jane Doe",
        "email": "jane.doe@example.com",
        "tel": "9876543210",
    }

    # Create contact
    await nc_client.contacts.create_contact(
        addressbook=addressbook_name,
        uid=contact_uid,
        contact_data=contact_data,
    )

    # Verify contact was created by listing
    contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
    contact_uids = [c["vcard_id"] for c in contacts]
    assert contact_uid in contact_uids

    # Delete contact
    await nc_client.contacts.delete_contact(
        addressbook=addressbook_name, uid=contact_uid
    )

    # Verify contact was deleted
    contacts = await nc_client.contacts.list_contacts(addressbook=addressbook_name)
    contact_uids = [c["vcard_id"] for c in contacts]
    assert contact_uid not in contact_uids
