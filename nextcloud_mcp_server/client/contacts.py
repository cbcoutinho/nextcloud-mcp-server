"""CardDAV client for NextCloud contacts operations."""

import logging
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

from pythonvCard4.vcard import Contact

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


# Canonical keys accepted by _build_contact_from_data. Callers normalise aliases
# (``phone``→``tel``, ``organization``→``org``) via _normalize_contact_data beforehand
# so the set never needs to list them.
_SUPPORTED_CONTACT_KEYS = frozenset(
    {
        "fn",
        "email",
        "tel",
        "org",
        "note",
        "title",
        "nickname",
        "bday",
        "categories",
        "url",
    }
)


def _normalize_contact_data(contact_data: dict[str, Any]) -> dict[str, Any]:
    """Map documented aliases to canonical keys.

    ``phone`` → ``tel``, ``organization`` → ``org``. The canonical key wins if both
    are supplied, so callers who set ``tel`` don't lose it to a stray ``phone`` entry.
    Returns a new dict — does not mutate the caller's argument.
    """
    normalised = dict(contact_data)
    if "phone" in normalised and "tel" not in normalised:
        normalised["tel"] = normalised.pop("phone")
    else:
        normalised.pop("phone", None)
    if "organization" in normalised and "org" not in normalised:
        normalised["org"] = normalised.pop("organization")
    else:
        normalised.pop("organization", None)
    return normalised


def _wrap_contact_field(
    value: str | dict[str, Any] | list[str | dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Normalize an email/tel input into pythonvCard4's list-of-dicts shape.

    Accepts a plain string, a dict already in ``{value, type}`` form, or a list of
    either. Empty strings and dicts without a ``value`` key are dropped. Always
    returns a list (possibly empty).
    """
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and item.get("value"):
            types = item.get("type") or ["HOME"]
            out.append({"value": item["value"], "type": list(types)})
        elif isinstance(item, str) and item:
            out.append({"value": item, "type": ["HOME"]})
    return out


def _as_str_list(value: str | list[str]) -> list[str]:
    """Wrap a bare string in a list. Does NOT split on commas.

    Used for ORG/NICKNAME/URL where commas are part of the value (e.g.
    ``"Smith, Jones & Associates"``) and only the list wrapper is needed to
    prevent pythonvCard4 from iterating the string character-by-character.
    """
    return value if isinstance(value, list) else [value]


def _split_categories(value: str | list[str]) -> list[str]:
    """Normalise CATEGORIES input: a comma-separated string is split into a list.

    Unlike ORG/NICKNAME, CATEGORIES is canonically comma-separated in vCards
    (``CATEGORIES:a,b,c``) so splitting a bare string is the expected shape.
    Lists pass through unchanged — callers that already provide ``["a,b"]`` keep
    their exact item, no double-splitting.
    """
    if isinstance(value, list):
        return value
    return [v.strip() for v in value.split(",") if v.strip()]


def _build_contact_from_data(contact_data: dict[str, Any], uid: str) -> Contact:
    """Build a pythonvCard4 Contact from an MCP ``contact_data`` dict.

    Maps every key documented on ``nc_contacts_create_contact`` onto the underlying
    library, normalising shapes (list/str) to avoid pythonvCard4's char-by-char
    iteration of bare strings — see issue #716.

    Callers must pre-normalise aliases via ``_normalize_contact_data`` before
    invoking this helper; it assumes canonical keys only.
    """
    data = contact_data

    if not data.get("fn"):
        logger.warning(
            "contact_data missing required 'fn' field; pythonvCard4 may reject or "
            "produce an invalid vCard"
        )

    kwargs: dict[str, Any] = {"fn": data.get("fn"), "uid": uid}

    emails = _wrap_contact_field(data.get("email"))
    if emails:
        kwargs["email"] = emails

    tels = _wrap_contact_field(data.get("tel"))
    if tels:
        kwargs["tel"] = tels

    if data.get("org"):
        kwargs["org"] = _as_str_list(data["org"])

    if data.get("note"):
        kwargs["note"] = data["note"]

    if data.get("title"):
        kwargs["title"] = data["title"]

    if data.get("nickname"):
        kwargs["nickname"] = _as_str_list(data["nickname"])

    if data.get("categories"):
        kwargs["categories"] = _split_categories(data["categories"])

    if data.get("url"):
        kwargs["url"] = _as_str_list(data["url"])

    bday = data.get("bday")
    if bday:
        if isinstance(bday, date):
            kwargs["bday"] = bday
        elif isinstance(bday, str):
            try:
                kwargs["bday"] = date.fromisoformat(bday)
            except ValueError:
                logger.warning("Ignoring non-ISO bday value: %r", bday)

    unknown = set(data) - _SUPPORTED_CONTACT_KEYS
    if unknown:
        logger.debug("Ignoring unknown contact_data keys: %s", sorted(unknown))

    # kwargs built dynamically from contact_data; pythonvCard4's Contact typeshed
    # has specific typed params and doesn't accept **dict[str, Any].
    return Contact(**kwargs)  # type: ignore[arg-type]


class ContactsClient(BaseNextcloudClient):
    """Client for NextCloud CardDAV contact operations."""

    app_name = "contacts"

    def _get_carddav_base_path(self) -> str:
        """Helper to get the base CardDAV path for contacts."""
        return f"/remote.php/dav/addressbooks/users/{self.username}"

    async def list_addressbooks(self):
        """List all available addressbooks for the user."""

        carddav_path = self._get_carddav_base_path()

        propfind_body = """<?xml version="1.0" encoding="utf-8"?>
        <d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">
            <d:prop>
                <d:displayname/>
                <d:getctag />
            </d:prop>
        </d:propfind>"""

        headers = {
            # "Depth": "0",
            "Content-Type": "application/xml",
            "Accept": "application/xml",
        }

        response = await self._make_request(
            "PROPFIND", carddav_path, content=propfind_body, headers=headers
        )

        ns = {"d": "DAV:"}

        # logger.info(response.content)
        root = ET.fromstring(response.content)
        addressbooks = []
        for response_elem in root.findall(".//d:response", ns):
            href = response_elem.find(".//d:href", ns)
            if href is None:
                continue

            href_text = href.text or ""
            if not href_text.endswith("/"):
                continue  # Skip non-addressbook resources

            # Extract addressbook name from href
            addressbook_name = href_text.rstrip("/").split("/")[-1]
            if not addressbook_name or addressbook_name == self.username:
                continue

            # Get properties
            propstat = response_elem.find(".//d:propstat", ns)
            if propstat is None:
                continue

            prop = propstat.find(".//d:prop", ns)
            if prop is None:
                continue

            displayname_elem = prop.find(".//d:displayname", ns)
            displayname = (
                displayname_elem.text
                if displayname_elem is not None
                else addressbook_name
            )

            getctag_elem = prop.find(".//d:getctag", ns)
            getctag = getctag_elem.text if getctag_elem is not None else None

            addressbooks.append(
                {
                    "name": addressbook_name,
                    "display_name": displayname,
                    "getctag": getctag,
                }
            )

        logger.debug(f"Found {len(addressbooks)} addressbooks")
        return addressbooks

    async def create_addressbook(self, *, name: str, display_name: str):
        """Create a new addressbook."""
        carddav_path = self._get_carddav_base_path()
        url = f"{carddav_path}/{name}/"

        prop_body = f"""<?xml version="1.0" encoding="utf-8"?>
        <d:mkcol xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">
            <d:set>
                <d:prop>
                    <d:resourcetype>
                        <d:collection/>
                        <c:addressbook/>
                    </d:resourcetype>
                    <d:displayname>{display_name}</d:displayname>
                </d:prop>
            </d:set>
        </d:mkcol>"""

        headers = {
            "Content-Type": "application/xml",
        }

        await self._make_request("MKCOL", url, content=prop_body, headers=headers)

    async def delete_addressbook(self, *, name: str):
        """Delete an addressbook."""
        carddav_path = self._get_carddav_base_path()
        url = f"{carddav_path}/{name}/"
        await self._make_request("DELETE", url)

    async def create_contact(
        self, *, addressbook: str, uid: str, contact_data: dict[str, Any]
    ):
        """Create a new contact."""
        carddav_path = self._get_carddav_base_path()
        url = f"{carddav_path}/{addressbook}/{uid}.vcf"

        # Normalise aliases here so the helper's invariant (canonical keys only) holds.
        contact_data = _normalize_contact_data(contact_data)
        vcard = _build_contact_from_data(contact_data, uid).to_vcard()

        headers = {
            "Content-Type": "text/vcard; charset=utf-8",
            "If-None-Match": "*",
        }

        await self._make_request("PUT", url, content=vcard, headers=headers)

    async def delete_contact(self, *, addressbook: str, uid: str):
        """Delete a contact."""
        carddav_path = self._get_carddav_base_path()
        url = f"{carddav_path}/{addressbook}/{uid}.vcf"
        await self._make_request("DELETE", url)

    async def update_contact(
        self,
        *,
        addressbook: str,
        uid: str,
        contact_data: dict[str, Any],
        etag: str = "",
    ):
        """Update an existing contact while preserving all existing properties."""
        carddav_path = self._get_carddav_base_path()
        url = f"{carddav_path}/{addressbook}/{uid}.vcf"

        # Canonicalise aliases up front so both code paths (merge + fallback) agree.
        contact_data = _normalize_contact_data(contact_data)

        # Get raw vCard content to preserve all properties including extended ones
        raw_vcard_content = ""
        if not etag:
            try:
                raw_vcard_content, current_etag = await self._get_raw_vcard(
                    addressbook, uid
                )
                etag = current_etag
            except Exception:
                # Fall back to creating new vCard if we can't get existing
                logger.warning(
                    f"Could not fetch existing vCard for {uid}, creating new"
                )
                raw_vcard_content = ""

        # Create updated vCard preserving existing properties
        if raw_vcard_content:
            vcard_content = self._merge_vcard_properties(
                raw_vcard_content, contact_data, uid
            )
        else:
            # Fallback to creating new vCard if we couldn't get existing
            vcard_content = _build_contact_from_data(contact_data, uid).to_vcard()

        headers = {
            "Content-Type": "text/vcard; charset=utf-8",
        }
        if etag:
            headers["If-Match"] = etag

        await self._make_request("PUT", url, content=vcard_content, headers=headers)

    async def list_contacts(self, *, addressbook: str):
        """List all available contacts for addressbook."""

        carddav_path = self._get_carddav_base_path()

        report_body = """<?xml version="1.0" encoding="utf-8"?>
        <card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
            <d:prop>
                <d:getetag />
                <card:address-data />
            </d:prop>
        </card:addressbook-query>"""

        headers = {
            "Depth": "1",
            "Content-Type": "application/xml",
            "Accept": "application/xml",
        }

        response = await self._make_request(
            "REPORT",
            f"{carddav_path}/{addressbook}",
            content=report_body,
            headers=headers,
        )

        ns = {"d": "DAV:", "card": "urn:ietf:params:xml:ns:carddav"}

        # logger.info(response.text)
        root = ET.fromstring(response.content)
        contacts = []
        for response_elem in root.findall(".//d:response", ns):
            href = response_elem.find(".//d:href", ns)
            if href is None:
                logger.info("Skip missing href")
                continue

            href_text = href.text or ""
            # logger.info("Href text: %s", href_text)
            # if not href_text.endswith("/"):
            # logger.info("# Skip non-addressbook resources")
            # continue

            # Extract vcard id from href
            vcard_id = href_text.rstrip("/").split("/")[-1]
            if not vcard_id:
                logger.info("Skip missing vcard_id")
                continue
            vcard_id = vcard_id.replace(".vcf", "")

            # Get properties
            propstat = response_elem.find(".//d:propstat", ns)
            if propstat is None:
                logger.info("Skip missing propstat")
                continue

            prop = propstat.find(".//d:prop", ns)
            if prop is None:
                logger.info("Skip missing prop")
                continue

            getetag_elem = prop.find(".//d:getetag", ns)
            getetag = getetag_elem.text if getetag_elem is not None else None

            addressdata_elem = prop.find(".//card:address-data", ns)
            addressdata = (
                addressdata_elem.text if addressdata_elem is not None else None
            )
            if addressdata is None:
                logger.info("Skip missing addressdata")
                continue

            contact = Contact.from_vcard(addressdata)

            contacts.append(
                {
                    "vcard_id": vcard_id,
                    "getetag": getetag,
                    "contact": {
                        "fullname": contact.fn,
                        "nickname": contact.nickname,
                        "birthday": contact.bday.isoformat()
                        if isinstance(contact.bday, date)
                        else contact.bday,
                        "email": contact.email,
                        "tel": contact.tel,
                    },
                    "addressdata": addressdata,
                }
            )

        logger.debug(f"Found {len(contacts)} contacts")
        return contacts

    async def _get_raw_vcard(self, addressbook: str, uid: str) -> tuple[str, str]:
        """Get raw vCard content for a contact without parsing."""
        carddav_path = self._get_carddav_base_path()
        url = f"{carddav_path}/{addressbook}/{uid}.vcf"

        try:
            response = await self._make_request("GET", url)
            etag = response.headers.get("etag", "")
            return response.text, etag
        except Exception as e:
            logger.error(f"Error getting raw vCard for {uid}: {e}")
            raise

    def _merge_vcard_properties(
        self, raw_vcard: str, contact_data: dict[str, Any], uid: str
    ) -> str:
        """Merge new contact data into existing raw vCard while preserving all properties."""
        try:
            # Instead of using pythonvCard4 which has formatting issues,
            # let's do a simple text-based merge to preserve exact formatting

            # Start with the original vCard
            lines = raw_vcard.strip().split("\n")
            updated_lines = []

            # Track what we've updated to avoid duplicates
            updated_properties = set()

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Skip the END:VCARD line for now
                if line == "END:VCARD":
                    continue

                property_name = line.split(":")[0].split(";")[0]

                # Handle updates for specific properties
                if property_name == "FN" and "fn" in contact_data:
                    updated_lines.append(f"FN:{contact_data['fn']}")
                    updated_properties.add("fn")
                elif property_name == "EMAIL" and "email" in contact_data:
                    # Replace first email with new one, preserve others
                    if "email" not in updated_properties:
                        if isinstance(contact_data["email"], str):
                            # Try to preserve the original format as much as possible
                            if ";TYPE=" in line:
                                type_part = line.split(";TYPE=")[1].split(":")[0]
                                updated_lines.append(
                                    f"EMAIL;TYPE={type_part}:{contact_data['email']}"
                                )
                            else:
                                updated_lines.append(f"EMAIL:{contact_data['email']}")
                        updated_properties.add("email")
                    else:
                        # Keep additional emails unchanged
                        updated_lines.append(line)
                elif property_name == "TEL" and "tel" in contact_data:
                    # Similar handling for phone numbers
                    if "tel" not in updated_properties:
                        if isinstance(contact_data["tel"], str):
                            if ";TYPE=" in line:
                                type_part = line.split(";TYPE=")[1].split(":")[0]
                                updated_lines.append(
                                    f"TEL;TYPE={type_part}:{contact_data['tel']}"
                                )
                            else:
                                updated_lines.append(f"TEL:{contact_data['tel']}")
                        updated_properties.add("tel")
                    else:
                        # Keep additional phone numbers unchanged
                        updated_lines.append(line)
                elif property_name == "NOTE" and "note" in contact_data:
                    updated_lines.append(f"NOTE:{contact_data['note']}")
                    updated_properties.add("note")
                elif property_name == "NICKNAME" and "nickname" in contact_data:
                    nickname_value = contact_data["nickname"]
                    if isinstance(nickname_value, list):
                        nickname_value = ",".join(nickname_value)
                    updated_lines.append(f"NICKNAME:{nickname_value}")
                    updated_properties.add("nickname")
                elif property_name == "BDAY" and "bday" in contact_data:
                    updated_lines.append(f"BDAY:{contact_data['bday']}")
                    updated_properties.add("bday")
                elif property_name == "CATEGORIES" and "categories" in contact_data:
                    categories_value = contact_data["categories"]
                    if isinstance(categories_value, list):
                        categories_value = ",".join(categories_value)
                    updated_lines.append(f"CATEGORIES:{categories_value}")
                    updated_properties.add("categories")
                elif property_name == "ORG" and (
                    "org" in contact_data or "organization" in contact_data
                ):
                    org_value = contact_data.get("org") or contact_data.get(
                        "organization"
                    )
                    updated_lines.append(f"ORG:{org_value}")
                    updated_properties.add("org")
                elif property_name == "TITLE" and "title" in contact_data:
                    updated_lines.append(f"TITLE:{contact_data['title']}")
                    updated_properties.add("title")
                elif property_name == "URL" and "url" in contact_data:
                    if "url" not in updated_properties:
                        url_value = contact_data["url"]
                        # Only the first URL from a list is written; multi-URL
                        # contacts are rare and this text merge doesn't attempt
                        # position-stable mapping to existing URL lines.
                        if isinstance(url_value, list):
                            url_value = url_value[0] if url_value else ""
                        if url_value:
                            updated_lines.append(f"URL:{url_value}")
                        updated_properties.add("url")
                    else:
                        # Keep additional URLs unchanged
                        updated_lines.append(line)
                else:
                    # Keep all other properties unchanged (preserves all extended/custom fields)
                    updated_lines.append(line)

            # Add any new properties that weren't in the original vCard
            for key, value in contact_data.items():
                if key not in updated_properties:
                    if key == "fn":
                        updated_lines.append(f"FN:{value}")
                    elif key == "email" and isinstance(value, str):
                        updated_lines.append(f"EMAIL:{value}")
                    elif key == "tel" and isinstance(value, str):
                        updated_lines.append(f"TEL:{value}")
                    elif key == "note":
                        updated_lines.append(f"NOTE:{value}")
                    elif key == "nickname":
                        nickname_value = (
                            value if isinstance(value, str) else ",".join(value)
                        )
                        updated_lines.append(f"NICKNAME:{nickname_value}")
                    elif key == "bday":
                        updated_lines.append(f"BDAY:{value}")
                    elif key == "categories":
                        categories_value = (
                            value if isinstance(value, str) else ",".join(value)
                        )
                        updated_lines.append(f"CATEGORIES:{categories_value}")
                    elif key in ["org", "organization"]:
                        updated_lines.append(f"ORG:{value}")
                    elif key == "title":
                        updated_lines.append(f"TITLE:{value}")
                    elif key == "url":
                        # Only the first URL is written on add-new; see note in the
                        # update-existing branch above.
                        url_value = (
                            value[0] if isinstance(value, list) and value else value
                        )
                        if url_value:
                            updated_lines.append(f"URL:{url_value}")

            # Add the END:VCARD line
            updated_lines.append("END:VCARD")

            # Join all lines
            return "\n".join(updated_lines)

        except Exception as e:
            logger.error(f"Error merging vCard properties: {e}")
            # Fallback to creating basic vCard matching Nextcloud format
            basic_vcard = f"""BEGIN:VCARD
VERSION:3.0
UID:{uid}
FN:{contact_data.get("fn", "Unknown")}"""

            if "email" in contact_data:
                basic_vcard += f"\nEMAIL:{contact_data['email']}"
            if "tel" in contact_data:
                basic_vcard += f"\nTEL:{contact_data['tel']}"

            basic_vcard += "\nEND:VCARD"
            return basic_vcard
