import logging
from datetime import date
from typing import Any

from httpx import HTTPStatusError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.contacts import (
    AddressBook,
    Contact,
    ContactField,
    ListAddressBooksResponse,
    ListContactsResponse,
    UpdateContactResponse,
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


# RFC 6350 §6.3.1: PO box, extended address, street, locality, region, postal
# code, country. Fixed at seven so `ContactField.components` can promise a stable
# shape regardless of how many parts the producing client emitted.
_ADR_COMPONENT_COUNT = 7


def _parse_address_fields(raw_values: dict | list | None) -> list[ContactField]:
    """Parse pythonvCard4's ADR shape into ContactField entries.

    The library yields ``[{'value': [7 components], 'type': ['HOME', 'PREF']}]``
    — verified to be a list even for a single ADR. A bare dict is normalised
    anyway so this stays symmetric with :func:`_parse_vcard_fields`, which
    handles the same polymorphism for EMAIL/TEL; relying on the list shape alone
    would fail silently (iterating a dict yields its keys, each skipped by the
    isinstance check) if the library ever changed.

    ``value`` is joined with ';' for a flat display string while ``components``
    keeps the structured form, so a consumer isn't forced to re-split it.
    """
    if not raw_values:
        return []
    if isinstance(raw_values, dict):
        raw_values = [raw_values]

    fields: list[ContactField] = []
    for item in raw_values:
        if not isinstance(item, dict):
            continue
        components = item.get("value") or []
        if isinstance(components, str):
            components = components.split(";")
        components = [str(c) for c in components]
        if not any(c.strip() for c in components):
            continue
        # Pad (or truncate) to exactly the seven RFC 6350 §6.3.1 components, so a
        # consumer can index `components[6]` for country without length-checking.
        # Lenient real-world producers emit fewer parts; the model documents seven,
        # and an unpadded short list would silently break that contract.
        components = (components + [""] * _ADR_COMPONENT_COUNT)[:_ADR_COMPONENT_COUNT]
        raw_types: list[str] = item.get("type") or []
        preferred = any(t.upper() == "PREF" for t in raw_types)
        labels = [t.lower() for t in raw_types if t.upper() != "PREF"]
        fields.append(
            ContactField(
                type="address",
                value=";".join(components),
                label=", ".join(labels) if labels else None,
                preferred=preferred,
                components=components,
            )
        )
    return fields


def _parse_url_fields(raw_urls: str | list | None) -> list[ContactField]:
    """Wrap URL values into ContactFields.

    pythonvCard4 parses URL into a plain ``list[str]``; single-string inputs
    surface as such too.
    """
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls] if raw_urls else []
    return [
        ContactField(type="url", value=u)
        for u in (raw_urls or [])
        if isinstance(u, str) and u
    ]


def _parse_categories(raw_categories: str | list | None) -> list[str]:
    """Normalise CATEGORIES to a list of non-empty strings.

    Parsed as ``list[str]``; a comma-separated string is also accepted for
    forward-compat with library updates that might change shape.
    """
    raw_categories = raw_categories or []
    if isinstance(raw_categories, str):
        return [c.strip() for c in raw_categories.split(",") if c.strip()]
    return [c for c in raw_categories if isinstance(c, str) and c]


def _build_custom_fields(contact_info: dict) -> dict[str, Any]:
    """Merge the nickname and any X-* extensions into one custom-field map.

    The ``nickname`` key is lower-case and extension keys are upper-case
    property names, so they cannot collide.
    """
    custom_fields: dict[str, Any] = {}
    nickname = contact_info.get("nickname")
    if nickname:
        custom_fields["nickname"] = nickname
    custom_fields.update(contact_info.get("custom") or {})
    return custom_fields


def _split_name_parts(raw_n: list | None) -> tuple[str | None, str | None]:
    """Return ``(family_name, given_name)`` from the N component list.

    N is ``[family, given, additional, prefix, suffix]``; index-guarded since a
    malformed card can carry fewer components. Empty strings become ``None``.
    """
    name_parts = raw_n or []
    family = name_parts[0] if len(name_parts) > 0 else None
    given = name_parts[1] if len(name_parts) > 1 else None
    return (family or None), (given or None)


def _raw_contact_to_model(raw: dict) -> Contact:
    """Convert a raw contact dict from the contacts client to a Contact model.

    Maps fullname, name parts, nickname, birthday, email, tel, address, org,
    title, note, url, categories, photo and X-* extension fields. Email/tel
    values may be plain strings, dicts with ``value``/``type`` keys, or lists of
    either – see :func:`_parse_vcard_fields`.
    """
    contact_info = raw.get("contact", {})

    emails = _parse_vcard_fields(contact_info.get("email"), "email")
    phones = _parse_vcard_fields(contact_info.get("tel"), "phone")
    urls = _parse_url_fields(contact_info.get("url"))
    categories = _parse_categories(contact_info.get("categories"))
    custom_fields = _build_custom_fields(contact_info)
    family_name, given_name = _split_name_parts(contact_info.get("n"))

    return Contact(
        uid=raw["vcard_id"],
        resource_path=raw.get("object_path"),
        fn=contact_info.get("fullname", ""),
        etag=raw.get("getetag"),
        given_name=given_name,
        family_name=family_name,
        organization=contact_info.get("org"),
        title=contact_info.get("title"),
        note=contact_info.get("note"),
        photo=contact_info.get("photo"),
        birthday=contact_info["birthday"].isoformat()
        if isinstance(contact_info.get("birthday"), date)
        else contact_info.get("birthday"),
        emails=emails,
        phones=phones,
        addresses=_parse_address_fields(contact_info.get("adr")),
        urls=urls,
        categories=categories,
        custom_fields=custom_fields,
    )


def configure_contacts_tools(mcp: MCPServer):
    # Contacts tools
    @mcp.tool(
        title="List Address Books",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
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
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
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
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
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
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
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
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
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
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
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
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
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
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("contacts.write")
    @instrument_tool
    async def nc_contacts_update_contact(
        ctx: Context, *, addressbook: str, uid: str, contact_data: dict, etag: str = ""
    ) -> UpdateContactResponse:
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
                  strings only**. Dict / list-form inputs are not applied — the
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
                  Non-ISO strings are rejected with a warning. The existing
                  BDAY line is preserved.
                - ``categories`` (list of str, or comma-separated str).
                - ``url`` (str or list of str). Only the first URL is written
                  on update. Multi-URL contacts should use create_contact.

                Example: ``{"fn": "Jane Doe", "email": "jane.doe@example.com"}``.
            etag: Optional ETag for optimistic concurrency control. Pass the
                value from a previous read or update. The update is rejected if
                the contact changed since.

        Returns:
            The updated contact and its new ETag, so updates can be chained
            without an intervening read.
        """
        client = await get_client(ctx)
        try:
            raw = await client.contacts.update_contact(
                addressbook=addressbook, uid=uid, contact_data=contact_data, etag=etag
            )
        except HTTPStatusError as e:
            if e.response.status_code == 412:
                raise ToolError(
                    f"Contact {uid!r} changed since etag {etag!r} was read. "
                    "Re-read it and retry the update."
                ) from e
            raise

        return UpdateContactResponse(
            contact=_raw_contact_to_model(raw),
            addressbook=addressbook,
        )
