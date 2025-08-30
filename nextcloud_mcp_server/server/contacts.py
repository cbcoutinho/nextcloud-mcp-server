import logging

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)


def configure_contacts_tools(mcp: FastMCP):
    # Contacts tools
    @mcp.tool()
    async def nc_contacts_list_addressbooks(ctx: Context):
        """List all addressbooks for the user."""
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.contacts.list_addressbooks()

    @mcp.tool()
    async def nc_contacts_list_contacts(ctx: Context, *, addressbook: str):
        """List all contacts in the specified addressbook."""
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.contacts.list_contacts(addressbook=addressbook)

    @mcp.tool()
    async def nc_contacts_create_addressbook(
        ctx: Context, *, name: str, display_name: str
    ):
        """Create a new addressbook.

        Args:
            name: The name of the addressbook.
            display_name: The display name of the addressbook.
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.contacts.create_addressbook(
            name=name, display_name=display_name
        )

    @mcp.tool()
    async def nc_contacts_delete_addressbook(ctx: Context, *, name: str):
        """Delete an addressbook."""
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.contacts.delete_addressbook(name=name)

    @mcp.tool()
    async def nc_contacts_create_contact(
        ctx: Context, *, addressbook: str, uid: str, contact_data: dict
    ):
        """Create a new contact.

        Args:
            addressbook: The name of the addressbook to create the contact in.
            uid: The unique ID for the contact.
            contact_data: A dictionary with the contact's details, e.g. {"fn": "John Doe", "email": "john.doe@example.com"}.
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.contacts.create_contact(
            addressbook=addressbook, uid=uid, contact_data=contact_data
        )

    @mcp.tool()
    async def nc_contacts_delete_contact(ctx: Context, *, addressbook: str, uid: str):
        """Delete a contact."""
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.contacts.delete_contact(addressbook=addressbook, uid=uid)
