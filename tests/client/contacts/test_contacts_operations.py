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

    logger.info("Found %s addressbooks", len(addressbooks))

    # Check structure of addressbooks
    for addressbook in addressbooks:
        assert "name" in addressbook
        assert "display_name" in addressbook
        assert "getctag" in addressbook

        logger.info(
            "Addressbook: %s - %s", addressbook["name"], addressbook["display_name"]
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


async def test_delete_contact_without_vcf_extension(
    nc_client: NextcloudClient, temporary_addressbook: str
):
    """Regression for issue #874: a contact whose CardDAV object filename has no
    ``.vcf`` extension (like the stock ``default`` sample contact) must still be
    deletable via the public API.

    ``create_contact`` can't reproduce the precondition — it always PUTs to
    ``<uid>.vcf`` — so seed the object directly at a bare path, then exercise
    ``list_contacts`` (real path exposure) and ``delete_contact`` (resolution).
    """
    addressbook = temporary_addressbook
    contacts = nc_client.contacts
    object_name = f"noext-{uuid.uuid4().hex[:8]}"  # filename WITHOUT .vcf
    carddav_path = contacts._get_carddav_base_path()
    vcard = (
        "BEGIN:VCARD\r\nVERSION:3.0\r\n"
        f"UID:{object_name}\r\nFN:No Ext\r\nEMAIL:noext@example.com\r\n"
        "END:VCARD\r\n"
    )

    # Seed the pathological object at a path that lacks the .vcf extension.
    await contacts._make_request(
        "PUT",
        f"{carddav_path}/{addressbook}/{object_name}",
        content=vcard,
        headers={"Content-Type": "text/vcard; charset=utf-8"},
    )

    try:
        # list_contacts surfaces it and exposes the real object path/name.
        listed = await contacts.list_contacts(addressbook=addressbook)
        match = next((c for c in listed if c["vcard_id"] == object_name), None)
        assert match is not None, "seeded no-.vcf contact not listed"
        assert match["object_name"] == object_name  # no .vcf extension
        assert match["object_path"].endswith(f"/{addressbook}/{object_name}")

        # Delete via the public API — pre-#874 this hit <uid>.vcf and 404'd.
        await contacts.delete_contact(addressbook=addressbook, uid=object_name)

        remaining = await contacts.list_contacts(addressbook=addressbook)
        assert object_name not in [c["vcard_id"] for c in remaining]
    finally:
        # Best-effort cleanup in case the assertions above failed before delete.
        try:
            await contacts._make_request(
                "DELETE", f"{carddav_path}/{addressbook}/{object_name}"
            )
        except Exception:
            pass


async def test_create_contact_persists_all_documented_fields(
    nc_client: NextcloudClient, temporary_addressbook: str
):
    """Regression for issue #716: org/note/phone/organization must persist to the vCard.

    Historically ``create_contact`` only handled fn/email/tel and silently dropped every
    other key. Inspect the raw server-side vCard (not just the parsed list response) to
    confirm each documented field round-trips.
    """
    addressbook_name = temporary_addressbook
    contact_uid = f"test-full-{uuid.uuid4().hex[:8]}"
    contact_data = {
        "fn": "Full Field User",
        "email": "full@example.com",
        "phone": "555-0716",  # alias for tel
        "organization": "Acme Corp",  # alias for org
        "note": "Issue 716 regression",
        "title": "Engineer",
        "url": "https://example.com",
    }

    await nc_client.contacts.create_contact(
        addressbook=addressbook_name,
        uid=contact_uid,
        contact_data=contact_data,
    )
    try:
        # create_contact always writes <uid>.vcf, so fetch that object directly
        # (no PROPFIND resolution needed for a contact we just created).
        raw_vcard, _etag = await nc_client.contacts._fetch_raw_vcard(
            addressbook_name, f"{contact_uid}.vcf"
        )
        assert "FN:Full Field User" in raw_vcard
        assert "EMAIL" in raw_vcard and "full@example.com" in raw_vcard
        assert "TEL" in raw_vcard and "555-0716" in raw_vcard
        assert "ORG:Acme Corp" in raw_vcard
        assert "NOTE:Issue 716 regression" in raw_vcard
        assert "TITLE:Engineer" in raw_vcard
        # Sabre rewrites bare URL: to URL;VALUE=URI: on PUT
        assert "URL" in raw_vcard and "https://example.com" in raw_vcard
    finally:
        await nc_client.contacts.delete_contact(
            addressbook=addressbook_name, uid=contact_uid
        )
