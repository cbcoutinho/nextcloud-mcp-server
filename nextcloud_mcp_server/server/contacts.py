import logging
from datetime import date
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

    Maps fullname, nickname, birthday, email, tel, org, title, note, url,
    categories, and photo fields. Email/tel values may be plain strings, dicts
    with ``value``/``type`` keys, or lists of either – see
    :func:`_parse_vcard_fields`.
    """
    contact_info = raw.get("contact", {})

    emails = _parse_vcard_fields(contact_info.get("email"), "email")
    phones = _parse_vcard_fields(contact_info.get("tel"), "phone")

    # URL is parsed by pythonvCard4 into a plain ``list[str]``. Single-string
    # inputs surface as such too. Either way wrap each into a ContactField.
    raw_urls = contact_info.get("url")
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls] if raw_urls else []
    urls = [
        ContactField(type="url", value=u)
        for u in (raw_urls or [])
        if isinstance(u, str) and u
    ]

    # CATEGORIES is parsed as ``list[str]``. Accept a comma-separated string
    # too for forward-compat with library updates that might change shape.
    raw_categories = contact_info.get("categories") or []
    if isinstance(raw_categories, str):
        categories = [c.strip() for c in raw_categories.split(",") if c.strip()]
    else:
        categories = [c for c in raw_categories if isinstance(c, str) and c]

    # Nickname goes into custom_fields (no dedicated model field)
    custom_fields: dict[str, Any] = {}
    nickname = contact_info.get("nickname")
    if nickname:
        custom_fields["nickname"] = nickname

    return Contact(
        uid=raw["vcard_id"],
        fn=contact_info.get("fullname", ""),
        etag=raw.get("getetag"),
        organization=contact_info.get("org"),
        title=contact_info.get("title"),
        note=contact_info.get("note"),
        photo=contact_info.get("photo"),
        birthday=contact_info["birthday"].isoformat()
        if isinstance(contact_info.get("birthday"), date)
        else contact_info.get("birthday"),
        emails=emails,
        phones=phones,
        urls=urls,
        categories=categories,
        custom_fields=custom_fields,
    )


def configure_contacts_tools(mcp: FastMCP):
    # Contacts tools
    @mcp.tool(
        title="List Address Books",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("contacts.read")
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
    @require_scopes("contacts.read")
    @instrument_tool
    async def nc_contacts_list_contacts(
        ctx: Context, *, addressbook: str
    ) -> ListContactsResponse:
        """List all contacts in the specified addressbook.

        Args:
            addressbook: The URI slug of the addressbook (e.g. "contacts"),
                not the display name. Use nc_contacts_list_addressbooks to
                find available URI slugs.
        """
        client = await get_client(ctx)
        contacts_data = await client.contacts.list_contacts(addressbook=addressbook)
        contacts = [_raw_contact_to_model(c) for c in contacts_data]
        return ListContactsResponse(
            contacts=contacts, addressbook=addressbook, total_count=len(contacts)
        )

    @mcp.tool(
        title="Search Contacts",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    @require_scopes("contacts.read")
    @instrument_tool
    async def nc_contacts_search_contacts(
        ctx: Context, *, query: str, addressbook: str | None = None
    ) -> ListContactsResponse:
        """Search contacts by free-text query across name, nickname, email, and phone.

        The query is matched case-insensitively as a substring against:
        - the contact's full name (FN)
        - any nickname
        - every email address
        - every phone number (digits only — formatting is stripped before
          comparison so '+1 234 567 890' matches '2345678' and '234.567.890')

        Args:
            query: Free-text search string (case-insensitive substring match).
                An empty query returns no results — use list_contacts for that.
            addressbook: Optional URI slug of a specific addressbook to search.
                When omitted, every addressbook for the user is searched.

        Returns:
            ListContactsResponse with matching contacts. The ``addressbook``
            field is set to the searched addressbook, or ``"*"`` when all
            addressbooks were searched.
        """
        client = await get_client(ctx)
        needle = (query or "").strip().lower()
        if not needle:
            return ListContactsResponse(
                contacts=[], addressbook=addressbook or "*", total_count=0
            )

        # Phone numbers are normalised to digits-only for comparison so that
        # users can search for "2345678" and find "+1 234-567-8" etc.
        digits_needle = "".join(ch for ch in needle if ch.isdigit())

        if addressbook:
            address_books = [addressbook]
        else:
            address_books = [
                ab["name"] for ab in await client.contacts.list_addressbooks()
            ]

        matches: list[Contact] = []
        for ab_slug in address_books:
            raw_contacts = await client.contacts.list_contacts(addressbook=ab_slug)
            for raw in raw_contacts:
                contact = _raw_contact_to_model(raw)
                hay_parts: list[str] = []
                if contact.fn:
                    hay_parts.append(contact.fn.lower())
                nickname = (
                    contact.custom_fields.get("nickname")
                    if contact.custom_fields
                    else None
                )
                if nickname:
                    hay_parts.append(str(nickname).lower())
                for e in contact.emails:
                    hay_parts.append(e.value.lower())
                hay = " ".join(hay_parts)

                phone_digits = "".join(
                    "".join(ch for ch in p.value if ch.isdigit())
                    for p in contact.phones
                )

                if needle in hay:
                    matches.append(contact)
                elif digits_needle and digits_needle in phone_digits:
                    matches.append(contact)

        return ListContactsResponse(
            contacts=matches,
            addressbook=addressbook or "*",
            total_count=len(matches),
        )

    @mcp.tool(
        title="Create Address Book",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("contacts.write")
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
    @require_scopes("contacts.write")
    @instrument_tool
    async def nc_contacts_delete_addressbook(ctx: Context, *, name: str):
        """Delete an addressbook."""
        client = await get_client(ctx)
        return await client.contacts.delete_addressbook(name=name)

    @mcp.tool(
        title="Create Contact",
        annotations=ToolAnnotations(idempotentHint=False, openWorldHint=True),
    )
    @require_scopes("contacts.write")
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
            contact_data: A dictionary with the contact's details. Supported keys:

                - ``fn`` (str, required): Formatted full name.
                - ``email`` (str or list of str/dicts): Email address(es).
                - ``tel`` / ``phone`` (str or list): Phone number(s).
                - ``org`` / ``organization`` (str or list of str): Organization.
                  Lists become semicolon-separated ORG components per RFC 6350.
                - ``title`` (str): Job title.
                - ``note`` (str): Free-form note.
                - ``nickname`` (str or list of str).
                - ``bday`` (ISO date str ``"YYYY-MM-DD"`` or ``datetime.date``).
                - ``categories`` (list of str, or comma-separated str).
                - ``url`` (str or list of str).

                Unknown keys are ignored. Example:
                ``{"fn": "John Doe", "email": "john@example.com",
                "organization": "Acme", "note": "Met at conference"}``.
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
    @require_scopes("contacts.write")
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
    @require_scopes("contacts.write")
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
            contact_data: A dictionary with the contact's updated details. Supported
                keys mirror nc_contacts_create_contact:

                - ``fn`` (str): Formatted full name.
                - ``email`` (str): Email address. **Update path supports plain
                  strings only**; dict / list-form inputs are not applied — the
                  existing EMAIL line is preserved unchanged and a warning is
                  logged. Use create_contact for multi-entry support with TYPE
                  annotations.
                - ``tel`` / ``phone`` (str): Phone number. Same single-string
                  limitation as ``email`` above.
                - ``org`` / ``organization`` (str or list of str): Organization.
                  Lists become semicolon-separated ORG components per RFC 6350.
                - ``title`` (str): Job title.
                - ``note`` (str): Free-form note.
                - ``nickname`` (str or list of str).
                - ``bday`` (ISO date str ``"YYYY-MM-DD"`` or ``datetime.date``).
                  Non-ISO strings are rejected with a warning; the existing
                  BDAY line is preserved.
                - ``categories`` (list of str, or comma-separated str).
                - ``url`` (str or list of str). Only the first URL is written
                  on update; multi-URL contacts should use create_contact.

                Example: ``{"fn": "Jane Doe", "email": "jane.doe@example.com"}``.
            etag: Optional ETag for optimistic concurrency control.
        """
        client = await get_client(ctx)
        return await client.contacts.update_contact(
            addressbook=addressbook, uid=uid, contact_data=contact_data, etag=etag
        )
