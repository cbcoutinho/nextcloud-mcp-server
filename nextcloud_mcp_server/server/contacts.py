import logging

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client

logger = logging.getLogger(__name__)


def configure_contacts_tools(mcp: FastMCP):
    # Contacts tools
    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_contacts_list_addressbooks(ctx: Context):
        """List all addressbooks for the user."""
        client = get_client(ctx)
        return await client.contacts.list_addressbooks()

    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_contacts_list_contacts(ctx: Context, *, addressbook: str):
        """List all contacts in the specified addressbook."""
        client = get_client(ctx)
        return await client.contacts.list_contacts(addressbook=addressbook)

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_contacts_create_addressbook(
        ctx: Context, *, name: str, display_name: str
    ):
        """Create a new addressbook.

        Args:
            name: The name of the addressbook.
            display_name: The display name of the addressbook.
        """
        client = get_client(ctx)
        return await client.contacts.create_addressbook(
            name=name, display_name=display_name
        )

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_contacts_delete_addressbook(ctx: Context, *, name: str):
        """Delete an addressbook."""
        client = get_client(ctx)
        return await client.contacts.delete_addressbook(name=name)

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_contacts_create_contact(
        ctx: Context, *, addressbook: str, uid: str, contact_data: dict
    ):
        """Create a new contact.

        Args:
            addressbook: The name of the addressbook to create the contact in.
            uid: The unique ID for the contact.
            contact_data: A dictionary with the contact's details, e.g. {"fn": "John Doe", "email": "john.doe@example.com"}.
        """
        client = get_client(ctx)
        return await client.contacts.create_contact(
            addressbook=addressbook, uid=uid, contact_data=contact_data
        )

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_contacts_delete_contact(ctx: Context, *, addressbook: str, uid: str):
        """Delete a contact."""
        client = get_client(ctx)
        return await client.contacts.delete_contact(addressbook=addressbook, uid=uid)

    @mcp.tool()
    @require_scopes("nc:write")
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
        client = get_client(ctx)
        return await client.contacts.update_contact(
            addressbook=addressbook, uid=uid, contact_data=contact_data, etag=etag
        )
