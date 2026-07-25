"""CardDAV client for NextCloud contacts operations."""

import logging
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

from pythonvCard4.vcard import Contact, fold_line, unfold_lines

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
            # Wrap a bare string so ``list("WORK")`` doesn't iterate it into
            # ``["W", "O", "R", "K"]`` — same char-iteration footgun this whole
            # helper exists to avoid for the outer ``value``.
            if isinstance(types, str):
                types = [types]
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


def _parse_bday(value: str | date | None) -> date | None:
    """Parse a BDAY input to a ``date``. Logs and returns ``None`` if unparseable.

    Shared by the create path (``_build_contact_from_data``) and the update path
    (``_merge_vcard_properties``) so a non-ISO BDAY is rejected consistently
    instead of being written raw on update.
    """
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            logger.warning("Ignoring non-ISO bday value: %r", value)
    return None


def _first_custom(custom: dict[str, str | list[str]], key: str) -> str | None:
    """Return the first raw value pythonvCard4 stashed in ``custom[key]``.

    The library has no typed parser for ORG / TITLE / unencoded PHOTO, so they
    end up in ``Contact.custom`` keyed by property name. The library's typeshed
    declares the values as ``str | list[str]`` even though the current parser
    always appends to a list — accept both shapes so we don't break on a future
    library version that switches to bare strings. Returns ``None`` when the
    key is absent or the value is empty.
    """
    values = custom.get(key)
    if isinstance(values, list):
        return values[0] if values else None
    if isinstance(values, str):
        return values or None
    return None


def _split_property_head(line: str) -> tuple[str, str]:
    """Split a logical vCard line into its group prefix and upper-cased name.

    ``FN:Alice``                   -> ``("", "FN")``
    ``email;TYPE=WORK:a@b``        -> ``("", "EMAIL")``
    ``item1.URL:https://x``        -> ``("item1.", "URL")``

    Property names are case-insensitive per RFC 6350 §3.3, and Apple-produced
    cards group related properties (``item1.URL`` + ``item1.X-ABLabel``). Matching
    on the raw text missed both shapes, so a lower-cased ``fn:`` or a grouped
    ``item1.EMAIL:`` was treated as unknown — preserved verbatim *and* then
    duplicated by the add-new loop. The group prefix is returned so a rewritten
    line can keep it and stay attached to its ``X-ABLabel``.
    """
    head = line.split(":", 1)[0].split(";", 1)[0]
    group, sep, name = head.rpartition(".")
    return group + sep, name.upper()


def _existing_type_param(line: str) -> str | None:
    """Return the raw ``TYPE=`` parameter value of a logical line, or ``None``.

    Parameter names are case-insensitive (RFC 6350 §3.3) just like property
    names, so the lookup runs on an upper-cased copy while the value is sliced
    from the original to preserve its casing. A case-sensitive ``";TYPE=" in
    line`` test would miss ``email;type=work:`` — which, now that property names
    match case-insensitively, *is* reached by the EMAIL branch and would have its
    TYPE parameter silently dropped on rewrite.
    """
    marker = ";TYPE="
    index = line.upper().find(marker)
    if index == -1:
        return None
    return line[index + len(marker) :].split(":")[0]


def _project_contact(contact: Contact) -> dict[str, Any]:
    """Project a parsed vCard into the flat dict the server layer maps to a model.

    Single projection point shared by ``list_contacts`` and ``update_contact`` so
    the read-after-write shape cannot drift from the read shape.

    pythonvCard4's parser has no branch for ORG / TITLE — they fall through into
    ``contact.custom`` as a list of raw values. PHOTO only gets typed-parsed when
    the line carries an ``ENCODING=`` parameter; otherwise it lands in ``custom``
    too. Pull them out here so the read side surfaces what the write side
    persisted (issue #716 follow-up).
    """
    return {
        "fullname": contact.fn,
        "nickname": contact.nickname,
        "birthday": contact.bday.isoformat()
        if isinstance(contact.bday, date)
        else contact.bday,
        "email": contact.email,
        "tel": contact.tel,
        "org": _first_custom(contact.custom, "ORG"),
        "title": _first_custom(contact.custom, "TITLE"),
        "note": contact.note,
        "url": contact.url,
        "categories": contact.categories,
        "photo": contact.photo_data or _first_custom(contact.custom, "PHOTO"),
    }


def _safe_vcard_value(value: Any) -> Any:
    """Escape newlines in a value so it can't inject additional vCard properties.

    Per RFC 6350 §3.4 newlines inside a property value are encoded as ``\\n``.
    Unfolding this on the read side is pythonvCard4's job; we only need to make
    sure ``contact_data`` strings don't terminate the line on the way out.
    """
    if isinstance(value, str):
        return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return value


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

    bday = _parse_bday(data.get("bday"))
    if bday is not None:
        kwargs["bday"] = bday

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
        return f"/remote.php/dav/addressbooks/users/{self._principal_or_username()}"

    async def _list_object_names(self, addressbook: str) -> list[str]:
        """Return the CardDAV object filenames stored in ``addressbook``.

        A lightweight ``PROPFIND`` (Depth: 1, ``getetag`` only) over the
        collection. The DAV object filename is independent of the vCard's
        internal ``UID`` and is *not* guaranteed to be ``<uid>.vcf`` — see
        issue #874 — so callers that need to address a specific object must
        discover its real name rather than constructing one.
        """
        await self._ensure_principal_id()
        carddav_path = self._get_carddav_base_path()
        propfind_body = """<?xml version="1.0" encoding="utf-8"?>
        <d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>"""
        headers = {
            "Depth": "1",
            "Content-Type": "application/xml",
            "Accept": "application/xml",
        }
        response = await self._make_request(
            "PROPFIND",
            f"{carddav_path}/{addressbook}",
            content=propfind_body,
            headers=headers,
        )

        ns = {"d": "DAV:"}
        root = ET.fromstring(response.content)
        names: list[str] = []
        for response_elem in root.findall(".//d:response", ns):
            href = response_elem.find(".//d:href", ns)
            if href is None or not href.text:
                continue
            # The collection itself is reported with a trailing slash; skip it
            # so only contact objects remain. The guard means href.text never
            # ends with "/" below, so the bare split is sufficient.
            if href.text.endswith("/"):
                continue
            names.append(href.text.split("/")[-1])
        return names

    async def _resolve_object_name(self, addressbook: str, uid: str) -> str | None:
        """Map a surfaced contact id back to its real CardDAV object filename.

        ``list_contacts`` surfaces ``vcard_id`` with a trailing ``.vcf`` suffix
        stripped, so reverse that transform here: return the object whose
        filename reduces to ``uid``. The conventional ``<uid>.vcf`` is
        preferred when present (deterministic for the common case), otherwise
        the first matching name is returned. ``None`` when no object matches.
        """
        candidates = [
            name
            for name in await self._list_object_names(addressbook)
            if name.removesuffix(".vcf") == uid
        ]
        if not candidates:
            return None
        conventional = f"{uid}.vcf"
        return conventional if conventional in candidates else candidates[0]

    async def list_addressbooks(self):
        """List all available addressbooks for the user."""

        await self._ensure_principal_id()
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
            if (
                not addressbook_name
                or addressbook_name == self._principal_or_username()
            ):
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

        logger.debug("Found %s addressbooks", len(addressbooks))
        return addressbooks

    async def create_addressbook(self, *, name: str, display_name: str):
        """Create a new addressbook."""
        await self._ensure_principal_id()
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
        await self._ensure_principal_id()
        carddav_path = self._get_carddav_base_path()
        url = f"{carddav_path}/{name}/"
        await self._make_request("DELETE", url)

    async def create_contact(
        self, *, addressbook: str, uid: str, contact_data: dict[str, Any]
    ):
        """Create a new contact."""
        await self._ensure_principal_id()
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
        """Delete a contact regardless of its CardDAV object filename.

        The object filename is independent of the vCard ``UID`` and may lack a
        ``.vcf`` extension (e.g. the stock ``default`` sample contact), so the
        real object name is resolved before deleting rather than assuming
        ``<uid>.vcf`` (issue #874). Falls back to the conventional name when no
        object matches so a genuinely missing contact still surfaces a 404.
        """
        await self._ensure_principal_id()
        carddav_path = self._get_carddav_base_path()
        object_name = await self._resolve_object_name(addressbook, uid) or f"{uid}.vcf"
        url = f"{carddav_path}/{addressbook}/{object_name}"
        await self._make_request("DELETE", url)

    async def update_contact(
        self,
        *,
        addressbook: str,
        uid: str,
        contact_data: dict[str, Any],
        etag: str = "",
    ) -> dict[str, Any]:
        """Update an existing contact while preserving all existing properties.

        The existing vCard is **always** fetched and merged into. Previously the
        fetch was skipped whenever the caller supplied an ``etag``, so the
        concurrency-correct call silently fell through to a rebuild from the
        handful of supported keys — destroying PHOTO, ADR, N, X-*, IMPP, GEO and
        REV. There is no rebuild path any more: if the existing card cannot be
        read, this raises rather than replacing it with a synthesised one.

        Returns the same shape as one ``list_contacts`` entry, carrying the new
        ETag so callers can chain updates without a re-read. ``getetag`` is an
        empty string when the server omitted the header from the PUT response
        (some proxies strip it); chaining then requires a fresh read first.

        Failures before the PUT raise. Failures *after* it do not: once the write
        has landed, an error building the response projection would misreport a
        successful update, so ``contact`` degrades to ``{}`` and the raw
        ``addressdata`` plus the new ETag are still returned.
        """
        await self._ensure_principal_id()
        carddav_path = self._get_carddav_base_path()
        # Resolve the real object filename (may differ from ``<uid>.vcf``) so the
        # GET and the PUT target the same resource — see issue #874.
        object_name = await self._resolve_object_name(addressbook, uid) or f"{uid}.vcf"
        url = f"{carddav_path}/{addressbook}/{object_name}"

        contact_data = _normalize_contact_data(contact_data)

        # Always read the current card: it is the only source of the properties
        # this method has no vocabulary for. A failure here propagates — a raised
        # error is recoverable, a silent rebuild is not.
        raw_vcard_content, current_etag = await self._fetch_raw_vcard(
            addressbook, object_name
        )
        # The caller's etag wins: it is their concurrency assertion, and honouring
        # the freshly-read one instead would defeat the check they asked for.
        if not etag:
            etag = current_etag

        vcard_content = self._merge_vcard_properties(raw_vcard_content, contact_data)

        headers = {
            "Content-Type": "text/vcard; charset=utf-8",
        }
        if etag:
            headers["If-Match"] = etag

        response = await self._make_request(
            "PUT", url, content=vcard_content, headers=headers
        )

        # Re-parse the card we just wrote rather than issuing a second GET.
        #
        # Deliberately fail-open, unlike every other error path in this method:
        # the PUT has already landed, so raising here would report a *successful*
        # write as a failure and invite a pointless — possibly destructive —
        # retry. The contract this method exists to protect ("never silently lose
        # data") is already satisfied at this point; only the convenience
        # projection is at risk. The caller still gets the new ETag and the full
        # ``addressdata``, so nothing is withheld.
        try:
            contact_projection = _project_contact(Contact.from_vcard(vcard_content))
        except Exception:
            # Broad by intent: pythonvCard4 is third-party and its parse failure
            # modes aren't enumerable. Whatever it raises, the write stands.
            logger.exception(
                "Contact %s was updated successfully but the written vCard could "
                "not be re-parsed for the response projection; returning the raw "
                "card and ETag instead",
                uid,
            )
            contact_projection = {}

        return {
            "vcard_id": object_name.removesuffix(".vcf"),
            "object_path": url,
            "object_name": object_name,
            "getetag": response.headers.get("etag", ""),
            "contact": contact_projection,
            "addressdata": vcard_content,
        }

    async def list_contacts(self, *, addressbook: str):
        """List all available contacts for addressbook."""

        await self._ensure_principal_id()
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

            # The real CardDAV object: its full DAV path and bare filename. The
            # filename is independent of the vCard UID and may lack a ``.vcf``
            # extension, so preserve it verbatim for callers that need to
            # address the object reliably (issue #874).
            object_path = href_text
            object_name = href_text.rstrip("/").split("/")[-1]
            if not object_name:
                logger.info("Skip missing vcard_id")
                continue
            # ``vcard_id`` keeps the historical ``.vcf``-stripped form for
            # backward compatibility with callers that use it as the contact id.
            # Must use the same trailing-suffix strip as ``_resolve_object_name``
            # so the surface-then-resolve round-trip stays lossless (issue #874).
            vcard_id = object_name.removesuffix(".vcf")

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

            contacts.append(
                {
                    "vcard_id": vcard_id,
                    "object_path": object_path,
                    "object_name": object_name,
                    "getetag": getetag,
                    "contact": _project_contact(Contact.from_vcard(addressdata)),
                    "addressdata": addressdata,
                }
            )

        logger.debug("Found %s contacts", len(contacts))
        return contacts

    async def _fetch_raw_vcard(
        self, addressbook: str, object_name: str
    ) -> tuple[str, str]:
        """Fetch raw vCard content + etag for an already-resolved object name.

        Callers that only have a surfaced ``uid`` (not the real object name)
        must resolve it first via ``_resolve_object_name`` — see issue #874.
        ``update_contact`` does exactly that and passes the resolved name here.
        """
        await self._ensure_principal_id()
        carddav_path = self._get_carddav_base_path()
        url = f"{carddav_path}/{addressbook}/{object_name}"

        try:
            response = await self._make_request("GET", url)
            etag = response.headers.get("etag", "")
            return response.text, etag
        except Exception as e:
            logger.error("Error getting raw vCard for %s: %s", object_name, e)
            raise

    def _merge_vcard_properties(
        self, raw_vcard: str, contact_data: dict[str, Any]
    ) -> str:
        """Merge new contact data into existing raw vCard while preserving all properties.

        The card's own ``UID`` line is carried through from ``raw_vcard`` like any
        other unrecognised property, so no ``uid`` argument is needed. (One used to
        be required solely by the removed rebuild fallback.)

        Limitation: dict / list-form ``email`` and ``tel`` inputs are not applied
        by this text-merge path. Existing EMAIL/TEL lines are preserved unchanged,
        and no new lines are written for the dict/list inputs. Pass plain strings
        to update EMAIL/TEL here, or recreate via ``create_contact`` for full
        multi-entry support with TYPE annotations.

        Unrecognised properties are copied through verbatim, but a property this
        method *rewrites* is re-emitted with its name upper-cased — a lower-cased
        ``fn:`` comes back as ``FN:``. Property names are case-insensitive per
        RFC 6350 §3.3 so this is semantically identical, but it is a visible
        formatting change and the only place the "preserve exact formatting"
        intent above does not hold literally. Group prefixes and ``TYPE``
        parameters are preserved as written.

        Raises on any merge failure rather than substituting a synthesised card.
        This function previously caught every exception and returned a four-line
        vCard built from ``fn``/``email``/``tel``, which silently destroyed PHOTO,
        ADR, N, X-* and every other property while reporting success. A raised
        error is recoverable; a silent rebuild is not.
        """
        # Surface dict/list email/tel up front rather than silently no-op in the
        # add-new loop below (where the isinstance(value, str) guard skips them).
        for _key in ("email", "tel"):
            _value = contact_data.get(_key)
            if _value is not None and not isinstance(_value, str):
                logger.warning(
                    "update_contact: %s=%r (dict/list shape) is not applied via "
                    "the text-merge update path; existing %s lines are preserved "
                    "unchanged. Use a plain string to update %s here, or recreate "
                    "via create_contact for multi-entry support.",
                    _key,
                    _value,
                    _key.upper(),
                    _key.upper(),
                )
        # Instead of using pythonvCard4 which has formatting issues,
        # let's do a simple text-based merge to preserve exact formatting.
        #
        # Unfold first: RFC 6350 §3.2 continuation lines start with a single
        # space/tab and belong to the property above them. The previous code
        # split on "\n" and ``.strip()``ed each physical line, which stripped that
        # leading space and re-emitted the continuation as a standalone property —
        # corrupting every long NOTE/ADR and every base64 PHOTO on any update.
        # ``splitlines()`` also handles CRLF, which ``split("\n")`` left as a
        # trailing "\r" on every line.
        lines = unfold_lines(raw_vcard.strip().splitlines())
        updated_lines = []

        # Track what we've updated to avoid duplicates
        updated_properties = set()

        for line in lines:
            if not line.strip():
                continue

            group_prefix, property_name = _split_property_head(line)

            # Skip the END:VCARD line for now
            if property_name == "END":
                continue

            # Handle updates for specific properties
            if property_name == "FN" and "fn" in contact_data:
                updated_lines.append(
                    f"{group_prefix}FN:{_safe_vcard_value(contact_data['fn'])}"
                )
                updated_properties.add("fn")
            elif property_name == "EMAIL" and "email" in contact_data:
                # Replace first email with new one, preserve others
                if "email" not in updated_properties:
                    if isinstance(contact_data["email"], str):
                        email_value = _safe_vcard_value(contact_data["email"])
                        # Try to preserve the original format as much as possible
                        type_part = _existing_type_param(line)
                        if type_part is not None:
                            updated_lines.append(
                                f"{group_prefix}EMAIL;TYPE={type_part}:{email_value}"
                            )
                        else:
                            updated_lines.append(f"{group_prefix}EMAIL:{email_value}")
                        updated_properties.add("email")
                    else:
                        # Dict / list inputs aren't translatable to a single
                        # text-merge replacement; keep the original line so we
                        # don't silently drop the contact's email.
                        updated_lines.append(line)
                else:
                    # Keep additional emails unchanged
                    updated_lines.append(line)
            elif property_name == "TEL" and "tel" in contact_data:
                # Similar handling for phone numbers
                if "tel" not in updated_properties:
                    if isinstance(contact_data["tel"], str):
                        tel_value = _safe_vcard_value(contact_data["tel"])
                        type_part = _existing_type_param(line)
                        if type_part is not None:
                            updated_lines.append(
                                f"{group_prefix}TEL;TYPE={type_part}:{tel_value}"
                            )
                        else:
                            updated_lines.append(f"{group_prefix}TEL:{tel_value}")
                        updated_properties.add("tel")
                    else:
                        # Same reasoning as the EMAIL branch above: don't drop.
                        updated_lines.append(line)
                else:
                    # Keep additional phone numbers unchanged
                    updated_lines.append(line)
            elif property_name == "NOTE" and "note" in contact_data:
                updated_lines.append(
                    f"{group_prefix}NOTE:{_safe_vcard_value(contact_data['note'])}"
                )
                updated_properties.add("note")
            elif property_name == "NICKNAME" and "nickname" in contact_data:
                nickname_value = contact_data["nickname"]
                if isinstance(nickname_value, list):
                    nickname_value = ",".join(nickname_value)
                updated_lines.append(
                    f"{group_prefix}NICKNAME:{_safe_vcard_value(nickname_value)}"
                )
                updated_properties.add("nickname")
            elif property_name == "BDAY" and "bday" in contact_data:
                parsed_bday = _parse_bday(contact_data["bday"])
                if parsed_bday is not None:
                    updated_lines.append(
                        f"{group_prefix}BDAY:{parsed_bday.isoformat()}"
                    )
                    updated_properties.add("bday")
                else:
                    # Invalid input — keep the existing BDAY rather than
                    # writing a malformed line or silently dropping it.
                    updated_lines.append(line)
            elif property_name == "CATEGORIES" and "categories" in contact_data:
                categories_value = contact_data["categories"]
                if isinstance(categories_value, list):
                    categories_value = ",".join(categories_value)
                updated_lines.append(
                    f"{group_prefix}CATEGORIES:{_safe_vcard_value(categories_value)}"
                )
                updated_properties.add("categories")
            elif property_name == "ORG" and "org" in contact_data:
                org_value = contact_data["org"]
                # ORG is structured (Company;Department;…) per RFC 6350 §6.6.4;
                # join list components with ';' so callers using the same shape
                # ``_build_contact_from_data`` accepts don't get a Python repr.
                if isinstance(org_value, list):
                    org_value = ";".join(org_value)
                updated_lines.append(
                    f"{group_prefix}ORG:{_safe_vcard_value(org_value)}"
                )
                updated_properties.add("org")
            elif property_name == "TITLE" and "title" in contact_data:
                updated_lines.append(
                    f"{group_prefix}TITLE:{_safe_vcard_value(contact_data['title'])}"
                )
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
                        updated_lines.append(
                            f"{group_prefix}URL:{_safe_vcard_value(url_value)}"
                        )
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
                    updated_lines.append(f"FN:{_safe_vcard_value(value)}")
                elif key == "email" and isinstance(value, str):
                    updated_lines.append(f"EMAIL:{_safe_vcard_value(value)}")
                elif key == "tel" and isinstance(value, str):
                    updated_lines.append(f"TEL:{_safe_vcard_value(value)}")
                elif key == "note":
                    updated_lines.append(f"NOTE:{_safe_vcard_value(value)}")
                elif key == "nickname":
                    nickname_value = (
                        value if isinstance(value, str) else ",".join(value)
                    )
                    updated_lines.append(
                        f"NICKNAME:{_safe_vcard_value(nickname_value)}"
                    )
                elif key == "bday":
                    parsed_bday = _parse_bday(value)
                    if parsed_bday is not None:
                        updated_lines.append(f"BDAY:{parsed_bday.isoformat()}")
                elif key == "categories":
                    categories_value = (
                        value if isinstance(value, str) else ",".join(value)
                    )
                    updated_lines.append(
                        f"CATEGORIES:{_safe_vcard_value(categories_value)}"
                    )
                elif key == "org":
                    # See ORG note in update-existing branch above.
                    org_value = ";".join(value) if isinstance(value, list) else value
                    updated_lines.append(f"ORG:{_safe_vcard_value(org_value)}")
                elif key == "title":
                    updated_lines.append(f"TITLE:{_safe_vcard_value(value)}")
                elif key == "url":
                    # Only the first URL is written on add-new; see note in the
                    # update-existing branch above.
                    url_value = value[0] if isinstance(value, list) and value else value
                    if url_value:
                        updated_lines.append(f"URL:{_safe_vcard_value(url_value)}")

        # Add the END:VCARD line
        updated_lines.append("END:VCARD")

        # Re-fold on the way out and join with CRLF, so an updated card is framed
        # exactly like a created one (``Contact.to_vcard()``) instead of the LF
        # this used to emit. Caveat: ``fold_line`` counts characters, not octets,
        # so a multibyte line can exceed RFC 6350's 75-octet target — it never
        # splits a codepoint, Sabre accepts it, and using the library helper keeps
        # create and update on one folding rule rather than inventing a second.
        physical_lines: list[str] = []
        for logical_line in updated_lines:
            physical_lines.extend(fold_line(logical_line))
        return "\r\n".join(physical_lines) + "\r\n"
