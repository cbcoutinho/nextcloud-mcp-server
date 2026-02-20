import logging

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.contacts import (
    AddressBook,
    Contact,
    ContactField,
    ListAddressBooksResponse,
    ListContactsResponse,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


def configure_contacts_tools(mcp: FastMCP):
    # Contacts tools
    @mcp.tool(
        title="List Address Books",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("contacts:read")
    @instrument_tool
    async def nc_contacts_list_addressbooks(ctx: Context) -> ListAddressBooksResponse:
        """List all addressbooks for the user."""
        client = await get_client(ctx)
        addressbooks_data = await client.contacts.list_addressbooks()
        addressbooks = [
            AddressBook(
                uri=ab["name"],
                displayname=ab.get("display_name", ab["name"]),
                ctag=ab.get("getctag"),
            )
            for ab in addressbooks_data
        ]
        return ListAddressBooksResponse(
            addressbooks=addressbooks, total_count=len(addressbooks)
        )

    @mcp.tool(
        title="List Contacts",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("contacts:read")
    @instrument_tool
    async def nc_contacts_list_contacts(
        ctx: Context, *, addressbook: str
    ) -> ListContactsResponse:
        """List all contacts in the specified addressbook."""
        client = await get_client(ctx)
        contacts_data = await client.contacts.list_contacts(addressbook=addressbook)
        contacts = []
        for c in contacts_data:
            contact_info = c.get("contact", {})

            # Convert email field (str, list, or None) to list[ContactField]
            raw_email = contact_info.get("email")
            emails: list[ContactField] = []
            if isinstance(raw_email, list):
                emails = [ContactField(type="email", value=e) for e in raw_email if e]
            elif isinstance(raw_email, str) and raw_email:
                emails = [ContactField(type="email", value=raw_email)]

            # Nickname goes into custom_fields (no dedicated model field)
            custom_fields: dict[str, str] = {}
            nickname = contact_info.get("nickname")
            if nickname:
                custom_fields["nickname"] = nickname

            contacts.append(
                Contact(
                    uid=c["vcard_id"],
                    fn=contact_info.get("fullname", ""),
                    etag=c.get("getetag"),
                    birthday=contact_info.get("birthday"),
                    emails=emails,
                    custom_fields=custom_fields,
                )
            )
        return ListContactsResponse(
            contacts=contacts, addressbook=addressbook, total_count=len(contacts)
        )

    @mcp.tool(
        title="Create Address Book",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("contacts:write")
    @instrument_tool
    async def nc_contacts_create_addressbook(
        ctx: Context, *, name: str, display_name: str
    ):
        """Create a new addressbook.

        Args:
            name: The name of the addressbook.
            display_name: The display name of the addressbook.
        """
        client = await get_client(ctx)
        return await client.contacts.create_addressbook(
            name=name, display_name=display_name
        )

    @mcp.tool(
        title="Delete Address Book",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("contacts:write")
    @instrument_tool
    async def nc_contacts_delete_addressbook(ctx: Context, *, name: str):
        """Delete an addressbook."""
        client = await get_client(ctx)
        return await client.contacts.delete_addressbook(name=name)

    @mcp.tool(
        title="Create Contact",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("contacts:write")
    @instrument_tool
    async def nc_contacts_create_contact(
        ctx: Context, *, addressbook: str, uid: str, contact_data: dict
    ):
        """Create a new contact.

        Args:
            addressbook: The name of the addressbook to create the contact in.
            uid: The unique ID for the contact.
            contact_data: A dictionary with the contact's details, e.g. {"fn": "John Doe", "email": "john.doe@example.com"}.
        """
        client = await get_client(ctx)
        return await client.contacts.create_contact(
            addressbook=addressbook, uid=uid, contact_data=contact_data
        )

    @mcp.tool(
        title="Delete Contact",
        annotations=ToolAnnotations(
            destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    @require_scopes("contacts:write")
    @instrument_tool
    async def nc_contacts_delete_contact(ctx: Context, *, addressbook: str, uid: str):
        """Delete a contact."""
        client = await get_client(ctx)
        return await client.contacts.delete_contact(addressbook=addressbook, uid=uid)

    @mcp.tool(
        title="Update Contact",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("contacts:write")
    @instrument_tool
    async def nc_contacts_update_contact(
        ctx: Context, *, addressbook: str, uid: str, contact_data: dict, etag: str = ""
    ):
        """Update an existing contact while preserving all existing properties.

        Args:
            addressbook: The name of the addressbook containing the contact.
            uid: The unique ID of the contact to update.
            contact_data: A dictionary with the contact's updated details, e.g. {"fn": "Jane Doe", "email": "jane.doe@example.com"}.
            etag: Optional ETag for optimistic concurrency control.
        """
        client = await get_client(ctx)
        return await client.contacts.update_contact(
            addressbook=addressbook, uid=uid, contact_data=contact_data, etag=etag
        )
