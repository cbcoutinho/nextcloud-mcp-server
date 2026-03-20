import logging
from typing import Any

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


def _parse_vcard_fields(
    raw_values: str | dict | list | None, field_type: str
) -> list[ContactField]:
    """Parse polymorphic vCard field data into a list of ContactField.

    pythonvCard4 returns field values in several shapes:
    - ``str``  – plain value, e.g. ``"alice@example.com"``
    - ``dict`` – ``{'value': '...', 'type': ['HOME', 'PREF']}``
    - ``list`` – a list whose items are any of the above

    The ``PREF`` type parameter is treated as a *preferred* flag rather than a
    label.  All other type values are lowercased and joined with ``", "``.
    """
    if raw_values is None:
        return []

    items: list[str | dict] = (
        raw_values if isinstance(raw_values, list) else [raw_values]
    )

    fields: list[ContactField] = []
    for item in items:
        if isinstance(item, dict):
            value = str(item.get("value", ""))
            if not value:
                continue
            raw_types: list[str] = item.get("type") or []
            preferred = any(t.upper() == "PREF" for t in raw_types)
            labels = [t.lower() for t in raw_types if t.upper() != "PREF"]
            fields.append(
                ContactField(
                    type=field_type,
                    value=value,
                    label=", ".join(labels) if labels else None,
                    preferred=preferred,
                )
            )
        elif isinstance(item, str) and item:
            fields.append(ContactField(type=field_type, value=item))

    return fields


def _raw_contact_to_model(raw: dict) -> Contact:
    """Convert a raw contact dict from the contacts client to a Contact model.

    Maps fullname, nickname, birthday, email, and tel fields.
    Email/tel values may be plain strings, dicts with ``value``/``type`` keys,
    or lists of either – see :func:`_parse_vcard_fields`.
    """
    contact_info = raw.get("contact", {})

    emails = _parse_vcard_fields(contact_info.get("email"), "email")
    phones = _parse_vcard_fields(contact_info.get("tel"), "phone")

    # Nickname goes into custom_fields (no dedicated model field)
    custom_fields: dict[str, Any] = {}
    nickname = contact_info.get("nickname")
    if nickname:
        custom_fields["nickname"] = nickname

    return Contact(
        uid=raw["vcard_id"],
        fn=contact_info.get("fullname", ""),
        etag=raw.get("getetag"),
        birthday=contact_info.get("birthday"),
        emails=emails,
        phones=phones,
        custom_fields=custom_fields,
    )


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
                # ab["name"] is a short slug like "contacts", not a full CardDAV URI;
                # all tools use it as a path segment: f"{carddav_path}/{name}/"
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
    async def nc_contacts_list_contacts        ctx: Context,
        *,
        addressbook: str,
        query: str | None = None,
        limit: int | None = 50,
    ) -> ListContactsResponse:
        """List all contacts in the specified addressbook.

        Args:
            addressbook: The URI slug of the addressbook (e.g. "contacts"),
                not the display name. Use nc_contacts_list_addressbooks to
                find available URI slugs.
            query: Optional text to search by (matched server-side against vCard `FN`).
            limit: Maximum number of contacts to return (best-effort; defaults to 50).
        """
        client = await get_client(ctx)
        contacts_data = await client.contacts.list_contacts_query(
            addressbook=addressbook, query=query, limit=limit
        )
        contacts = [_raw_contact_to_model(c) for c in contacts_data]
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
            addressbook: The URI slug of the addressbook (e.g. "contacts"),
                not the display name. Use nc_contacts_list_addressbooks to
                find available URI slugs.
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
        """Delete a contact.

        Args:
            addressbook: The URI slug of the addressbook (e.g. "contacts"),
                not the display name. Use nc_contacts_list_addressbooks to
                find available URI slugs.
            uid: The unique ID of the contact to delete.
        """
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
            addressbook: The URI slug of the addressbook (e.g. "contacts"),
                not the display name. Use nc_contacts_list_addressbooks to
                find available URI slugs.
            uid: The unique ID of the contact to update.
            contact_data: A dictionary with the contact's updated details, e.g. {"fn": "Jane Doe", "email": "jane.doe@example.com"}.
            etag: Optional ETag for optimistic concurrency control.
        """
        client = await get_client(ctx)
        return await client.contacts.update_contact(
            addressbook=addressbook, uid=uid, contact_data=contact_data, etag=etag
        )
