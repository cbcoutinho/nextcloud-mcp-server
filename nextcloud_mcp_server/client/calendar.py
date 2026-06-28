"""CalDAV client for Nextcloud calendar and task operations using caldav library."""

import datetime as dt
import inspect
import logging
import uuid
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
import httpx
import recurring_ical_events
from caldav.aio import AsyncCalendar, AsyncDAVClient, AsyncEvent
from caldav.elements import cdav, dav
from caldav.lib import error as caldav_error
from icalendar import Alarm, Calendar, Timezone, vDDDTypes, vRecur
from icalendar import Event as ICalEvent
from icalendar import Todo as ICalTodo
from lxml import etree  # type: ignore[import-untyped]

from ..config import get_nextcloud_ssl_verify

logger = logging.getLogger(__name__)


async def _maybe_await(result: Any) -> Any:
    """Await a result if it's a coroutine, otherwise return it directly.

    caldav v3 uses dual-mode methods that return coroutines for async clients
    but plain objects when the result is already available (e.g. load() on
    already-loaded objects).
    """
    if inspect.isawaitable(result):
        return await result
    return result


class CalendarClient:
    """Client for Nextcloud CalDAV calendar and task operations."""

    def __init__(
        self,
        base_url: str,
        username: str,
        *,
        auth_username: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ):
        """Initialize CalendarClient with AsyncDAVClient.

        Pass the raw credential plus an explicit ``auth_type`` so caldav can
        build whichever auth object its active HTTP backend needs. caldav v3
        prefers ``niquests`` over ``httpx`` and won't accept an ``httpx.Auth``
        when ``niquests`` is the active backend (issue #731), so we no longer
        accept a pre-built ``httpx.Auth`` here.

        Args:
            base_url: Nextcloud base URL
            username: Nextcloud username (UID) used as the DAV path fallback
            auth_username: Credential identity (loginName) the app password
                authenticates against; defaults to ``username``. Differs from
                the UID for OIDC-provisioned users.
            password: App password / login password — selects ``auth_type="basic"``
            token: OAuth bearer token — selects ``auth_type="bearer"``

        Pass exactly one of ``password`` or ``token``. Passing neither leaves
        the underlying client unauthenticated.
        """
        self.username = username
        self.base_url = base_url
        # The UID (``username``) is the DAV path fallback until principal
        # discovery succeeds; the loginName (``auth_username``) is the
        # credential the app password authenticates against. They differ for
        # OIDC-provisioned users. Defaults to the UID so existing single-user /
        # OAuth callers are unchanged.
        auth_username = auth_username or username

        auth_kwargs: dict[str, Any] = {}
        if password is not None:
            auth_kwargs = {"password": password, "auth_type": "basic"}
        elif token is not None:
            auth_kwargs = {"password": token, "auth_type": "bearer"}

        # AsyncDAVClient needs the full base URL for proper URL construction.
        #
        # The X-NC-CalDAV-Webcal-Caching header makes Nextcloud expose external
        # subscriptions (webcal/ICS feeds) as regular, queryable calendars
        # (CachedSubscription) instead of opaque cs:subscribed collections, so
        # their events become readable through the normal event/search tools —
        # the same mechanism desktop clients (Evolution/KDE) rely on (issue #830).
        # list_calendars() overrides this header to "Off" on its own PROPFIND so
        # it can still detect subscriptions and flag them read-only.
        self._dav_client = AsyncDAVClient(
            url=f"{base_url}/remote.php/dav/",
            username=auth_username,
            ssl_verify_cert=get_nextcloud_ssl_verify(),  # type: ignore[arg-type]  # caldav types say bool|str but passes through to niquests which accepts SSLContext
            headers={"X-NC-CalDAV-Webcal-Caching": "On"},
            **auth_kwargs,
        )
        self._calendar_home_url = f"{base_url}/remote.php/dav/calendars/{username}/"
        self._principal_resolved = False

    def _calendar_home_url_from_home_set(self, home_set: Any) -> str | None:
        """Normalize a caldav CalendarSet or URL into an absolute home URL."""
        if home_set is None:
            return None

        home_url = getattr(home_set, "url", home_set)
        if home_url is None:
            return None

        home_url = str(home_url)
        if not home_url:
            return None
        if home_url.startswith("/"):
            home_url = f"{self.base_url}{home_url}"
        if not home_url.endswith("/"):
            home_url = f"{home_url}/"
        return home_url

    async def _calendar_home_url_from_principal(self, principal: Any) -> str | None:
        """Resolve calendar-home-set without using caldav's async-unsafe property."""
        get_property = getattr(principal, "get_property", None)
        if get_property is not None:
            try:
                home_set = await _maybe_await(get_property(cdav.CalendarHomeSet()))
                calendar_home_url = self._calendar_home_url_from_home_set(home_set)
                if calendar_home_url:
                    return calendar_home_url
            except (caldav_error.DAVError, AttributeError, TypeError, ValueError) as e:
                logger.warning(
                    "CalDAV calendar-home-set discovery failed; deriving from "
                    "principal URL: %s",
                    e,
                )

        try:
            home_set = getattr(principal, "calendar_home_set", None)
            home_set = await _maybe_await(home_set)
            return self._calendar_home_url_from_home_set(home_set)
        except (AttributeError, TypeError, ValueError) as e:
            logger.warning(
                "CalDAV calendar-home-set property unavailable; deriving from "
                "principal URL: %s",
                e,
            )
            return None

    async def _ensure_calendar_home(self) -> None:
        """Discover and cache the authenticated user's CalDAV calendar home."""
        if self._principal_resolved:
            return

        try:
            get_principal = getattr(self._dav_client, "get_principal", None)
            if get_principal is None:
                principal = await _maybe_await(self._dav_client.principal())
            else:
                principal = await _maybe_await(get_principal())

            calendar_home_url = await self._calendar_home_url_from_principal(principal)
            if calendar_home_url:
                self._calendar_home_url = calendar_home_url
                self._principal_resolved = True
                return

            principal_url = getattr(principal, "url", None)
            if principal_url is None:
                raise ValueError("CalDAV principal discovery returned no URL")
            principal_id = unquote(str(principal_url).rstrip("/").split("/")[-1])
            if principal_id:
                self._calendar_home_url = (
                    f"{self.base_url}/remote.php/dav/calendars/{principal_id}/"
                )
                self._principal_resolved = True
        except (caldav_error.DAVError, httpx.HTTPError, ValueError) as e:
            logger.warning(
                "CalDAV principal discovery failed; using username path: %s", e
            )

    def _get_calendar_url(self, calendar_name: str) -> str:
        """Get the full URL for a calendar."""
        return f"{self._calendar_home_url}{calendar_name}/"

    def _get_calendar(self, calendar_name: str) -> AsyncCalendar:
        """Get an AsyncCalendar object for the given calendar name."""
        calendar_url = self._get_calendar_url(calendar_name)
        return AsyncCalendar(
            client=self._dav_client,  # type: ignore[arg-type]  # AsyncDAVClient is valid for async mode
            url=calendar_url,
            name=calendar_name,
        )

    async def _async_object_by_uid(
        self, calendar: AsyncCalendar, uid: str, comp_filter: Any = None
    ) -> Any:
        """Async version of Calendar.get_object_by_uid.

        Upstream caldav v3's get_object_by_uid is not async-aware: it calls
        search() which returns a coroutine for async clients, then tries to
        iterate the coroutine synchronously. This method properly awaits the
        search result.
        """
        # _hacks="insist" mirrors upstream's Calendar.get_object_by_uid pattern:
        # retries with per-component-type searches if the initial search returns
        # nothing, handling CalDAV servers with incomplete search support.
        items_found = await calendar.search(  # type: ignore[misc]  # dual-mode: returns coroutine for async clients
            uid=uid, xml=comp_filter, post_filter=True, _hacks="insist"
        )
        items_found = [o for o in items_found if o.id == uid]
        if not items_found:
            raise caldav_error.NotFoundError(f"{uid} not found on server")
        return items_found[0]

    async def close(self):
        """Close the DAV client connection."""
        await self._dav_client.close()

    async def _wait_for_calendar_propagation(
        self, calendar_name: str, max_attempts: int = 40, initial_delay_ms: int = 100
    ) -> None:
        """Wait for calendar to propagate through Nextcloud's DAV backend.

        After MKCALENDAR succeeds (201), the calendar may not be immediately queryable
        due to Nextcloud's internal caching/indexing. This polls until it appears.

        Args:
            calendar_name: Name of the calendar to wait for
            max_attempts: Maximum polling attempts (default: 40)
            initial_delay_ms: Initial delay between attempts in ms (default: 100ms)
        """
        logger.info("Waiting for calendar '%s' to propagate...", calendar_name)
        delay_ms = initial_delay_ms

        for attempt in range(max_attempts):
            try:
                logger.debug(
                    "Attempt %s/%s to find calendar '%s'...",
                    attempt + 1,
                    max_attempts,
                    calendar_name,
                )
                calendars = await self.list_calendars()
                if any(cal["name"] == calendar_name for cal in calendars):
                    logger.info(
                        "Calendar '%s' became available after %s attempts",
                        calendar_name,
                        attempt + 1,
                    )
                    return
            except Exception as e:
                logger.warning(
                    "Attempt %s/%s to verify calendar '%s' failed: %s",
                    attempt + 1,
                    max_attempts,
                    calendar_name,
                    e,
                )

            if attempt < max_attempts - 1:
                await anyio.sleep(delay_ms / 1000.0)
                # Exponential backoff: double delay up to 2 seconds max
                delay_ms = min(delay_ms * 2, 2000)

        logger.error(
            "Calendar '%s' did not become available after %s attempts.",
            calendar_name,
            max_attempts,
        )

    # ============= Calendar Operations =============

    async def list_calendars(self) -> list[dict[str, Any]]:
        """List all available calendars for the user.

        Returns both regular calendars and external read-only subscriptions
        (webcal/ICS feeds). Subscriptions are reported with ``read_only=True``
        and a ``source`` URL pointing at the upstream feed (issue #830).
        """
        await self._ensure_calendar_home()
        # Use custom PROPFIND with CalendarServer namespace (cs:) for calendar-color.
        # caldav library's nsmap lacks "CS" namespace, and its CalendarColor uses
        # Apple iCal namespace which Nextcloud doesn't recognize.
        #
        # cs:source / ical:calendar-color are requested to surface external
        # subscriptions: Nextcloud exposes those as cs:subscribed collections
        # carrying a cs:source href and an Apple-namespace color.
        propfind_body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:ical="http://apple.com/ns/ical/">
    <d:prop>
        <d:displayname/>
        <d:resourcetype/>
        <cs:getctag/>
        <c:calendar-description/>
        <cs:calendar-color/>
        <ical:calendar-color/>
        <cs:source/>
    </d:prop>
</d:propfind>"""

        # Override the client-wide webcal-caching header to "Off" for this
        # PROPFIND so subscriptions are returned as cs:subscribed collections
        # (with cs:source) and can be detected and flagged read-only. With the
        # header "On" they would masquerade as regular calendars, hiding the
        # source URL. Event reads keep the client-wide "On" so they stay
        # queryable (see __init__).
        # Pass the request XML via ``body``, not ``props``: caldav's ``props``
        # expects a list of property *names* and would build its own body
        # (discarding this custom CalendarServer/Apple-namespace markup).
        response = await self._dav_client.propfind(
            self._calendar_home_url,
            body=propfind_body,
            depth=1,
            headers={"X-NC-CalDAV-Webcal-Caching": "Off"},
        )

        result = []

        # Parse XML response
        tree = etree.fromstring(response.raw.encode("utf-8"))
        ns = {
            "d": "DAV:",
            "cs": "http://calendarserver.org/ns/",
            "c": "urn:ietf:params:xml:ns:caldav",
            "ical": "http://apple.com/ns/ical/",
        }

        for response_elem in tree.findall(".//d:response", ns):
            # A response is a calendar if it is a regular calendar collection
            # (c:calendar) or an external subscription (cs:subscribed).
            resourcetype = response_elem.find(".//d:resourcetype", ns)
            if resourcetype is None:
                continue
            is_calendar = resourcetype.find(".//c:calendar", ns) is not None
            is_subscribed = resourcetype.find(".//cs:subscribed", ns) is not None
            if not (is_calendar or is_subscribed):
                continue

            href = response_elem.find("./d:href", ns)
            if href is None or not href.text:
                continue

            calendar_url = href.text
            # Extract calendar name from URL
            calendar_name = calendar_url.rstrip("/").split("/")[-1]

            # Skip if this is the calendar home itself
            if calendar_url.rstrip("/") == self._calendar_home_url.rstrip("/"):
                continue

            display_name_elem = response_elem.find(".//d:displayname", ns)
            display_name = (
                display_name_elem.text
                if display_name_elem is not None and display_name_elem.text
                else calendar_name
            )

            description_elem = response_elem.find(".//c:calendar-description", ns)
            description = (
                description_elem.text
                if description_elem is not None and description_elem.text
                else ""
            )

            # Regular calendars expose cs:calendar-color; subscriptions store
            # their color under the Apple iCal namespace.
            color_elem = response_elem.find(".//cs:calendar-color", ns)
            if color_elem is None or not color_elem.text:
                color_elem = response_elem.find(".//ical:calendar-color", ns)
            color = (
                color_elem.text
                if color_elem is not None and color_elem.text
                else "#1976D2"
            )

            # External subscriptions carry a cs:source href pointing at the
            # upstream feed and are read-only.
            source = None
            source_elem = response_elem.find(".//cs:source", ns)
            if source_elem is not None:
                source_href = source_elem.find("./d:href", ns)
                if source_href is not None and source_href.text:
                    source = source_href.text
                elif source_elem.text and source_elem.text.strip():
                    source = source_elem.text.strip()

            result.append(
                {
                    "name": calendar_name,
                    "display_name": display_name,
                    "description": description,
                    "color": color,
                    "href": calendar_url,
                    "read_only": is_subscribed,
                    "source": source,
                }
            )

        logger.debug("Found %s calendars", len(result))
        return result

    async def create_calendar(
        self,
        calendar_name: str,
        display_name: str = "",
        description: str = "",
        color: str = "#1976D2",
    ) -> dict[str, Any]:
        """Create a new calendar with retry on 429 errors."""
        await self._ensure_calendar_home()
        # Use custom MKCALENDAR XML instead of caldav library's make_calendar() due to:
        # 1. Missing CalendarServer namespace (cs:) in caldav's nsmap
        # 2. caldav's CalendarColor uses Apple iCal namespace, not cs:calendar-color
        # 3. make_calendar() doesn't support calendar-description or calendar-color params
        calendar_url = self._get_calendar_url(calendar_name)

        mkcalendar_body = f"""<?xml version="1.0" encoding="utf-8"?>
<mkcalendar xmlns="urn:ietf:params:xml:ns:caldav" xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">
    <d:set>
        <d:prop>
            <d:displayname>{display_name or calendar_name}</d:displayname>
            <cs:calendar-color>{color}</cs:calendar-color>
            <caldav:calendar-description xmlns:caldav="urn:ietf:params:xml:ns:caldav">{description}</caldav:calendar-description>
            <caldav:supported-calendar-component-set xmlns:caldav="urn:ietf:params:xml:ns:caldav">
                <caldav:comp name="VEVENT"/>
                <caldav:comp name="VTODO"/>
            </caldav:supported-calendar-component-set>
        </d:prop>
    </d:set>
</mkcalendar>"""

        # Create calendar via MKCALENDAR request
        response = await self._dav_client.mkcalendar(calendar_url, mkcalendar_body)

        if response.status != 201:
            raise RuntimeError(
                f"Failed to create calendar '{calendar_name}': HTTP {response.status}"
            )

        logger.debug("Created calendar: %s", calendar_name)

        # Wait for calendar to be queryable (Nextcloud eventual consistency)
        await self._wait_for_calendar_propagation(calendar_name)

        return {
            "name": calendar_name,
            "display_name": display_name or calendar_name,
            "description": description,
            "color": color,
            "status_code": 201,
        }

    async def delete_calendar(self, calendar_name: str) -> dict[str, Any]:
        """Delete a calendar."""
        await self._ensure_calendar_home()
        # Use absolute URL for deletion
        calendar_url = self._get_calendar_url(calendar_name)
        await self._dav_client.delete(calendar_url)

        logger.debug("Deleted calendar: %s", calendar_name)
        return {"status_code": 204}

    # ============= Event Operations =============

    async def get_calendar_events(
        self,
        calendar_name: str,
        start_datetime: dt.datetime | None = None,
        end_datetime: dt.datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List events in a calendar within date range."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        if start_datetime or end_datetime:
            events = await self._search_events_by_date(
                calendar, start_datetime, end_datetime
            )
            # Client-side recurrence expansion preserves DTSTART format
            # (floating / TZID / UTC). RFC 4791 <C:expand> would normalize
            # everything to UTC and erase the original timezone context.
            do_expand = bool(start_datetime and end_datetime)
        else:
            events = await calendar.events()  # type: ignore[misc]  # dual-mode
            do_expand = False

        result = []
        for event in events:
            await _maybe_await(event.load(only_if_unloaded=True))
            if not event.data:
                continue

            try:
                cal = Calendar.from_ical(event.data)
            except Exception as e:
                logger.error("Error parsing iCalendar event: %s", e)
                continue

            href = str(event.url)
            event_dicts = self._expand_event_occurrences(
                cal, start_datetime, end_datetime, do_expand
            )
            for event_dict in event_dicts:
                event_dict["href"] = href
                event_dict["etag"] = ""
                result.append(event_dict)

                if len(result) >= limit:
                    break

            if len(result) >= limit:
                break

        logger.debug("Found %d events", len(result))
        return result

    def _expand_event_occurrences(
        self,
        cal: Any,
        start_datetime: dt.datetime | None,
        end_datetime: dt.datetime | None,
        do_expand: bool,
    ) -> list[dict[str, Any]]:
        """Return one event dict per occurrence in [start, end), or one dict for the master VEVENT.

        When ``do_expand`` is true and the resource has an RRULE, expand recurrences
        client-side using ``recurring_ical_events`` so that TZID and floating-local
        semantics are preserved on the wire (server-side ``<C:expand>`` would
        UTC-normalize every DTSTART per RFC 4791 §9.6.5).
        """
        if not do_expand:
            for component in cal.walk("VEVENT"):
                return [self._extract_vevent_data(component)]
            return []

        has_rrule = any("rrule" in component for component in cal.walk("VEVENT"))
        if not has_rrule:
            for component in cal.walk("VEVENT"):
                return [self._extract_vevent_data(component)]
            return []

        try:
            assert start_datetime is not None and end_datetime is not None
            occurrences = recurring_ical_events.of(cal).between(
                start_datetime, end_datetime
            )
        except Exception as e:
            logger.warning(
                "Client-side recurrence expansion failed (%s); returning master event",
                e,
            )
            return [
                self._extract_vevent_data(component) for component in cal.walk("VEVENT")
            ]

        return [self._extract_vevent_data(occ) for occ in occurrences]

    async def _search_events_by_date(
        self,
        calendar: AsyncCalendar,
        start_datetime: dt.datetime | None = None,
        end_datetime: dt.datetime | None = None,
    ) -> list:
        """Execute a CalDAV REPORT with time-range filter.

        Returns raw VEVENT resources (no server-side ``<C:expand>``). The caller
        is responsible for expanding recurring events client-side so that
        TZID/floating semantics are preserved.
        """
        # Ensure naive datetimes are treated as UTC for the wire-level filter
        if start_datetime and start_datetime.tzinfo is None:
            start_datetime = start_datetime.replace(tzinfo=dt.UTC)
        if end_datetime and end_datetime.tzinfo is None:
            end_datetime = end_datetime.replace(tzinfo=dt.UTC)

        # Build comp-filter with time-range (mirrors sync Calendar.build_search_xml_query)
        inner_comp_filter = cdav.CompFilter(name="VEVENT")
        inner_comp_filter += cdav.TimeRange(start_datetime, end_datetime)
        outer_comp_filter = cdav.CompFilter(name="VCALENDAR") + inner_comp_filter
        filter_element = cdav.Filter() + outer_comp_filter

        data = cdav.CalendarData()
        query = cdav.CalendarQuery() + [dav.Prop() + data] + filter_element

        body = etree.tostring(
            query.xmlelement(), encoding="utf-8", xml_declaration=True
        )
        assert calendar.client is not None
        response = await calendar.client.report(str(calendar.url), body, depth=1)  # type: ignore[misc]  # dual-mode

        # Parse response (same pattern as AsyncCalendar.search)
        objects = []
        response_data = response.expand_simple_props([cdav.CalendarData()])
        for href, props in response_data.items():
            if href == str(calendar.url):
                continue
            cal_data = props.get(cdav.CalendarData.tag)
            if cal_data:
                obj = AsyncEvent(
                    client=calendar.client,
                    url=calendar.url.join(href),  # type: ignore[union-attr]  # url is always set for calendars
                    data=cal_data,
                    parent=calendar,
                )
                objects.append(obj)

        return objects

    async def create_event(
        self, calendar_name: str, event_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a new calendar event."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        event_uid = str(uuid.uuid4())
        ical_content = self._create_ical_event(event_data, event_uid)

        # caldav v3's _async_put raises PutError on HTTP failure
        event = await calendar.save_event(ical=ical_content)  # type: ignore[misc]  # dual-mode

        logger.debug("Created event %s", event_uid)

        return {
            "uid": event_uid,
            "href": str(event.url),
            "etag": "",
            "status_code": 201,
        }

    async def update_event(
        self,
        calendar_name: str,
        event_uid: str,
        event_data: dict[str, Any],
        etag: str = "",
    ) -> dict[str, Any]:
        """Update an existing calendar event."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        # Find the event by UID using caldav library
        event = await self._async_object_by_uid(
            calendar, event_uid, cdav.CompFilter("VEVENT")
        )
        await _maybe_await(event.load(only_if_unloaded=True))

        # Merge updates into existing iCal data
        updated_ical = self._merge_ical_properties(event.data, event_data, event_uid)  # type: ignore[arg-type]
        event.data = updated_ical  # type: ignore[misc]

        await _maybe_await(event.save())

        logger.debug("Updated event %s", event_uid)
        return {
            "uid": event_uid,
            "href": str(event.url),
            "etag": "",
            "status_code": 200,
        }

    async def delete_event(self, calendar_name: str, event_uid: str) -> dict[str, Any]:
        """Delete a calendar event."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        try:
            event = await self._async_object_by_uid(
                calendar, event_uid, cdav.CompFilter("VEVENT")
            )
            await _maybe_await(event.delete())
            logger.debug("Deleted event %s", event_uid)
            return {"status_code": 204}
        except caldav_error.NotFoundError as e:
            logger.debug("Event %s not found: %s", event_uid, e)
            return {"status_code": 404}

    async def get_event(
        self, calendar_name: str, event_uid: str
    ) -> tuple[dict[str, Any], str]:
        """Get detailed information about a specific event."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        event = await self._async_object_by_uid(
            calendar, event_uid, cdav.CompFilter("VEVENT")
        )
        await _maybe_await(event.load(only_if_unloaded=True))

        event_data = self._parse_ical_event(event.data) if event.data else None  # type: ignore[arg-type]
        if not event_data:
            raise ValueError(f"Failed to parse event data for {event_uid}")

        event_data["href"] = str(event.url)
        event_data["etag"] = ""

        logger.debug("Retrieved event %s", event_uid)
        return event_data, ""

    async def search_events_across_calendars(
        self,
        start_datetime: dt.datetime | None = None,
        end_datetime: dt.datetime | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search events across all calendars with advanced filtering."""
        await self._ensure_calendar_home()
        try:
            calendars = await self.list_calendars()
            all_events = []

            for calendar in calendars:
                try:
                    events = await self.get_calendar_events(
                        calendar["name"], start_datetime, end_datetime
                    )

                    # Apply filters if provided
                    if filters:
                        events = self._apply_event_filters(events, filters)

                    # Add calendar info to each event
                    for event in events:
                        event["calendar_name"] = calendar["name"]
                        event["calendar_display_name"] = calendar.get(
                            "display_name", calendar["name"]
                        )

                    all_events.extend(events)
                except Exception as e:
                    logger.warning(
                        "Error getting events from calendar %s: %s", calendar["name"], e
                    )
                    continue

            return all_events

        except Exception as e:
            logger.error("Error searching events across calendars: %s", e)
            raise

    # ============= Todo/Task Operations (NEW) =============

    async def list_todos(
        self, calendar_name: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """List todos/tasks in a calendar."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        # Get all todos including completed ones (filtering is done client-side)
        todos = await calendar.todos(include_completed=True)  # type: ignore[misc]  # dual-mode

        result = []
        for todo in todos:
            # Only load if data not already present from REPORT response
            # This avoids 404 errors for virtual calendars (e.g., Deck boards)
            await _maybe_await(todo.load(only_if_unloaded=True))
            if todo.data:
                todo_dict = self._parse_ical_todo(todo.data)  # type: ignore[arg-type]
            else:
                continue
            if todo_dict:
                todo_dict["href"] = str(todo.url)
                todo_dict["etag"] = ""

                # Apply filters if provided
                if not filters or self._todo_matches_filters(todo_dict, filters):
                    result.append(todo_dict)

        logger.debug("Found %s todos", len(result))
        return result

    async def create_todo(
        self, calendar_name: str, todo_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a new todo/task."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        todo_uid = str(uuid.uuid4())
        ical_content = self._create_ical_todo(todo_data, todo_uid)

        # caldav v3's _async_put raises PutError on HTTP failure
        todo = await calendar.save_todo(ical=ical_content)  # type: ignore[misc]  # dual-mode

        logger.debug("Created todo %s", todo_uid)

        return {
            "uid": todo_uid,
            "href": str(todo.url),
            "etag": "",
            "status_code": 201,
        }

    async def update_todo(
        self,
        calendar_name: str,
        todo_uid: str,
        todo_data: dict[str, Any],
        etag: str = "",
    ) -> dict[str, Any]:
        """Update an existing todo/task."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        try:
            # Find the todo by UID
            todo = await self._async_object_by_uid(
                calendar, todo_uid, cdav.CompFilter("VTODO")
            )
            await _maybe_await(todo.load(only_if_unloaded=True))

            logger.debug(
                "Loaded todo %s, current data length: %s", todo_uid, len(todo.data)
            )

            # Merge updates into existing iCal data
            updated_ical = self._merge_ical_todo_properties(
                todo.data,  # type: ignore[arg-type]
                todo_data,
                todo_uid,
            )
            logger.debug("Merged iCal data length: %s", len(updated_ical))
            logger.debug("Updated iCal content:\\n%s", updated_ical)

            todo.data = updated_ical

            await _maybe_await(todo.save())

            logger.debug("Updated todo %s", todo_uid)
            return {
                "uid": todo_uid,
                "href": str(todo.url),
                "etag": "",
                "status_code": 200,
            }
        except Exception as e:
            logger.error("Error updating todo %s: %s", todo_uid, e, exc_info=True)
            raise

    async def delete_todo(self, calendar_name: str, todo_uid: str) -> dict[str, Any]:
        """Delete a todo/task."""
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        try:
            todo = await self._async_object_by_uid(
                calendar, todo_uid, cdav.CompFilter("VTODO")
            )
            await _maybe_await(todo.delete())
            logger.debug("Deleted todo %s", todo_uid)
            return {"status_code": 204}
        except caldav_error.NotFoundError as e:
            logger.debug("Todo %s not found: %s", todo_uid, e)
            return {"status_code": 404}

    async def search_todos_across_calendars(
        self, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Search todos across all calendars."""
        await self._ensure_calendar_home()
        try:
            calendars = await self.list_calendars()
            all_todos = []

            for calendar in calendars:
                try:
                    todos = await self.list_todos(calendar["name"], filters)

                    # Add calendar info to each todo
                    for todo in todos:
                        todo["calendar_name"] = calendar["name"]
                        todo["calendar_display_name"] = calendar.get(
                            "display_name", calendar["name"]
                        )

                    all_todos.extend(todos)
                except Exception as e:
                    logger.warning(
                        "Error getting todos from calendar %s: %s", calendar["name"], e
                    )
                    continue

            return all_todos

        except Exception as e:
            logger.error("Error searching todos across calendars: %s", e)
            raise

    # ============= Helper Methods - Event iCalendar =============

    @staticmethod
    def _resolve_timezone(tz_name: str) -> ZoneInfo | None:
        """Resolve an IANA timezone name to ZoneInfo, returning None for invalid input."""
        if not tz_name:
            return None
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Unknown IANA timezone %r — falling back to floating local time",
                tz_name,
            )
            return None

    @classmethod
    def _parse_event_datetime(
        cls, dt_str: str, tz_name: str | None = None
    ) -> tuple[dt.datetime, ZoneInfo | None]:
        """Parse an ISO datetime string with optional TZID application.

        Returns ``(parsed_dt, applied_zoneinfo)`` where ``applied_zoneinfo``
        is non-None only when ``tz_name`` was applied to a naive input — the
        caller uses this to know whether to emit a VTIMEZONE component.
        """
        parsed = dt.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        zi = cls._resolve_timezone(tz_name) if tz_name else None

        if parsed.tzinfo is not None:
            if zi is not None:
                logger.warning(
                    "Datetime %r has an explicit offset; ignoring timezone=%r",
                    dt_str,
                    tz_name,
                )
            return parsed, None

        if zi is not None:
            return parsed.replace(tzinfo=zi), zi

        logger.warning(
            "Datetime %r is naive and no timezone was supplied — storing as RFC 5545 floating local time",
            dt_str,
        )
        return parsed, None

    def _create_ical_event(self, event_data: dict[str, Any], event_uid: str) -> str:
        """Create iCalendar content from event data."""
        cal = Calendar()
        cal.add("prodid", "-//Nextcloud MCP Server//EN")
        cal.add("version", "2.0")

        event = ICalEvent()
        event.add("uid", event_uid)
        event.add("summary", event_data.get("title", ""))
        event.add("description", event_data.get("description", ""))
        event.add("location", event_data.get("location", ""))

        # Handle dates/times
        start_str = event_data.get("start_datetime", "")
        end_str = event_data.get("end_datetime", "")
        all_day = event_data.get("all_day", False)
        tz_name = event_data.get("timezone", "")
        used_timezones: set[ZoneInfo] = set()

        if start_str:
            if all_day:
                start_date = dt.datetime.fromisoformat(start_str.split("T")[0]).date()
                event.add("dtstart", start_date)
                if end_str:
                    end_date = dt.datetime.fromisoformat(end_str.split("T")[0]).date()
                    event.add("dtend", end_date)
            else:
                start_dt, zi = self._parse_event_datetime(start_str, tz_name)
                if zi is not None:
                    used_timezones.add(zi)
                event.add("dtstart", start_dt)
                if end_str:
                    end_dt, zi = self._parse_event_datetime(end_str, tz_name)
                    if zi is not None:
                        used_timezones.add(zi)
                    event.add("dtend", end_dt)

        # Add categories
        categories = event_data.get("categories", "")
        if categories:
            event.add("categories", [c.strip() for c in categories.split(",")])

        # Add priority and status
        priority = event_data.get("priority", 5)
        event.add("priority", priority)

        status = event_data.get("status", "CONFIRMED")
        event.add("status", status)

        # Add privacy classification
        privacy = event_data.get("privacy", "PUBLIC")
        event.add("class", privacy)

        # Add URL
        url = event_data.get("url", "")
        if url:
            event.add("url", url)

        # Handle recurrence
        recurring = event_data.get("recurring", False)
        if recurring:
            recurrence_rule = event_data.get("recurrence_rule", "")
            if recurrence_rule:
                event.add("rrule", vRecur.from_ical(recurrence_rule))

        # Add alarms/reminders
        reminder_minutes = event_data.get("reminder_minutes", 0)
        if reminder_minutes > 0:
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", "Event reminder")
            alarm.add("trigger", dt.timedelta(minutes=-reminder_minutes))
            event.add_component(alarm)

        # Add attendees
        attendees = event_data.get("attendees", "")
        if attendees:
            for email in attendees.split(","):
                if email.strip():
                    event.add("attendee", f"mailto:{email.strip()}")

        # Add timestamps
        now = dt.datetime.now(dt.UTC)
        event.add("created", now)
        event.add("dtstamp", now)
        event.add("last-modified", now)

        # VTIMEZONE must appear before the referencing VEVENT.
        for zi in used_timezones:
            cal.add_component(Timezone.from_tzinfo(zi))
        cal.add_component(event)
        return cal.to_ical().decode("utf-8")

    def _extract_vevent_data(self, component) -> dict[str, Any]:
        """Extract event data from a single VEVENT component."""
        event_data: dict[str, Any] = {
            "uid": str(component.get("uid", "")),
            "title": str(component.get("summary", "")),
            "description": str(component.get("description", "")),
            "location": str(component.get("location", "")),
            "status": str(component.get("status", "CONFIRMED")),
            "priority": int(component.get("priority", 5)),
            "privacy": str(component.get("class", "PUBLIC")),
            "url": str(component.get("url", "")),
        }

        # Handle dates. The ``.isoformat()`` representation already encodes the
        # storage semantics: no suffix for floating local, ``+00:00`` for UTC,
        # and the offset (e.g. ``-04:00``) for TZID-bound datetimes. The IANA
        # TZID name is surfaced separately as ``start_tz``/``end_tz`` so callers
        # can distinguish "10am NY time" (recurs in local time across DST) from
        # "14:00 UTC" (same UTC instant), which the offset alone cannot express.
        dtstart = component.get("dtstart")
        if dtstart:
            event_data["start_datetime"] = dtstart.dt.isoformat()
            event_data["all_day"] = isinstance(dtstart.dt, dt.date) and not isinstance(
                dtstart.dt, dt.datetime
            )
            tzid = dtstart.params.get("TZID") if dtstart.params else None
            if tzid:
                event_data["start_tz"] = str(tzid)

        dtend = component.get("dtend")
        if dtend:
            event_data["end_datetime"] = dtend.dt.isoformat()
            tzid = dtend.params.get("TZID") if dtend.params else None
            if tzid:
                event_data["end_tz"] = str(tzid)

        # Handle categories
        categories = component.get("categories")
        if categories:
            event_data["categories"] = self._extract_categories(categories)

        # Handle recurrence
        rrule = component.get("rrule")
        if rrule:
            event_data["recurring"] = True
            event_data["recurrence_rule"] = str(rrule)

        # Handle attendees
        attendees = []
        for attendee in component.get("attendee", []):
            if isinstance(attendee, list):
                attendees.extend(str(a).replace("mailto:", "") for a in attendee)
            else:
                attendees.append(str(attendee).replace("mailto:", ""))
        if attendees:
            event_data["attendees"] = ",".join(attendees)

        return event_data

    def _parse_ical_event(self, ical_text: str) -> dict[str, Any] | None:
        """Parse iCalendar text and extract the first event."""
        try:
            cal = Calendar.from_ical(ical_text)
            for component in cal.walk():
                if component.name == "VEVENT":
                    return self._extract_vevent_data(component)
            return None
        except Exception as e:
            logger.error("Error parsing iCalendar event: %s", e)
            return None

    def _merge_ical_properties(
        self, raw_ical: str, event_data: dict[str, Any], event_uid: str
    ) -> str:
        """Merge new event data into existing raw iCal while preserving all properties."""
        try:
            cal = Calendar.from_ical(raw_ical)

            for component in cal.walk():
                if component.name == "VEVENT":
                    # Update only provided properties
                    if "title" in event_data:
                        component["SUMMARY"] = event_data["title"]
                    if "description" in event_data:
                        component["DESCRIPTION"] = event_data["description"]
                    if "location" in event_data:
                        component["LOCATION"] = event_data["location"]
                    if "status" in event_data:
                        component["STATUS"] = event_data["status"].upper()
                    if "priority" in event_data:
                        component["PRIORITY"] = event_data["priority"]
                    if "privacy" in event_data:
                        component["CLASS"] = event_data["privacy"].upper()
                    if "url" in event_data:
                        component["URL"] = event_data["url"]

                    # Handle categories
                    if "categories" in event_data:
                        categories_str = event_data["categories"]
                        if categories_str:
                            component["CATEGORIES"] = [
                                c.strip() for c in categories_str.split(",")
                            ]
                        elif "CATEGORIES" in component:
                            del component["CATEGORIES"]

                    # Handle recurrence rule
                    if "recurrence_rule" in event_data:
                        rrule_str = event_data["recurrence_rule"]
                        if rrule_str:
                            component["RRULE"] = vRecur.from_ical(rrule_str)
                        elif "RRULE" in component:
                            del component["RRULE"]

                    # Handle attendees
                    if "attendees" in event_data:
                        attendees_str = event_data["attendees"]
                        # Remove all existing attendees first
                        while "ATTENDEE" in component:
                            del component["ATTENDEE"]
                        if attendees_str:
                            for email in attendees_str.split(","):
                                if email.strip():
                                    component.add("attendee", f"mailto:{email.strip()}")

                    # Handle reminder (VALARM)
                    if "reminder_minutes" in event_data:
                        component.subcomponents = [
                            sub
                            for sub in component.subcomponents
                            if sub.name != "VALARM"
                        ]
                        minutes = event_data["reminder_minutes"]
                        if minutes > 0:
                            alarm = Alarm()
                            alarm.add("action", "DISPLAY")
                            alarm.add("description", "Event reminder")
                            alarm.add("trigger", dt.timedelta(minutes=-minutes))
                            component.add_component(alarm)

                    # Handle dates
                    tz_name = event_data.get("timezone", "")
                    used_timezones: set[ZoneInfo] = set()
                    if "start_datetime" in event_data:
                        start_str = event_data["start_datetime"]
                        all_day = event_data.get("all_day", False)
                        if all_day:
                            start_date = dt.datetime.fromisoformat(
                                start_str.split("T")[0]
                            ).date()
                            component["DTSTART"] = vDDDTypes(start_date)
                        else:
                            start_dt, zi = self._parse_event_datetime(
                                start_str, tz_name
                            )
                            if zi is not None:
                                used_timezones.add(zi)
                            component["DTSTART"] = vDDDTypes(start_dt)

                    if "end_datetime" in event_data:
                        end_str = event_data["end_datetime"]
                        all_day = event_data.get("all_day", False)
                        if all_day:
                            end_date = dt.datetime.fromisoformat(
                                end_str.split("T")[0]
                            ).date()
                            component["DTEND"] = vDDDTypes(end_date)
                        else:
                            end_dt, zi = self._parse_event_datetime(end_str, tz_name)
                            if zi is not None:
                                used_timezones.add(zi)
                            component["DTEND"] = vDDDTypes(end_dt)

                    # Update timestamps
                    now = dt.datetime.now(dt.UTC)
                    component["LAST-MODIFIED"] = vDDDTypes(now)
                    component["DTSTAMP"] = vDDDTypes(now)

                    # Ensure VTIMEZONE definitions exist for any TZID we just attached.
                    existing_tzids = {
                        str(sub.get("TZID", ""))
                        for sub in cal.subcomponents
                        if sub.name == "VTIMEZONE"
                    }
                    for zi in used_timezones:
                        if str(zi) not in existing_tzids:
                            cal.add_component(Timezone.from_tzinfo(zi))

                    break

            return cal.to_ical().decode("utf-8")

        except Exception as e:
            logger.error("Error merging iCal properties: %s", e)
            return self._create_ical_event(event_data, event_uid)

    # ============= Helper Methods - Todo iCalendar =============

    def _ensure_timezone_aware(self, datetime_str: str) -> dt.datetime:
        """Parse datetime string and ensure it's timezone-aware.

        If the datetime string doesn't include timezone info, interpret it as UTC.
        This ensures RFC 5545 compliance for CalDAV/iCalendar properties.

        Args:
            datetime_str: ISO format datetime string (e.g., "2025-10-19T14:30:00" or "2025-10-19T14:30:00Z")

        Returns:
            Timezone-aware datetime object
        """
        # Replace 'Z' with '+00:00' for consistent parsing
        datetime_str = datetime_str.replace("Z", "+00:00")

        # Parse the datetime
        parsed_dt = dt.datetime.fromisoformat(datetime_str)

        # If timezone-naive, assume UTC
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=dt.UTC)

        return parsed_dt

    def _create_ical_todo(self, todo_data: dict[str, Any], todo_uid: str) -> str:
        """Create iCalendar VTODO content from todo data."""
        cal = Calendar()
        cal.add("prodid", "-//Nextcloud MCP Server//EN")
        cal.add("version", "2.0")

        todo = ICalTodo()
        todo.add("uid", todo_uid)
        todo.add("summary", todo_data.get("summary", ""))
        todo.add("description", todo_data.get("description", ""))

        # Status
        status = todo_data.get("status", "NEEDS-ACTION").upper()
        todo.add("status", status)

        # Priority (0-9, 0=undefined)
        priority = todo_data.get("priority", 0)
        todo.add("priority", priority)

        # Percent complete
        percent = todo_data.get("percent_complete", 0)
        todo.add("percent-complete", percent)

        # Due date
        due = todo_data.get("due", "")
        if due:
            due_dt = self._ensure_timezone_aware(due)
            todo.add("due", vDDDTypes(due_dt))

        # Start date
        dtstart = todo_data.get("dtstart", "")
        if dtstart:
            start_dt = self._ensure_timezone_aware(dtstart)
            todo.add("dtstart", vDDDTypes(start_dt))

        # Completed timestamp
        completed = todo_data.get("completed", "")
        if completed:
            completed_dt = self._ensure_timezone_aware(completed)
            todo.add("completed", vDDDTypes(completed_dt))

        # Categories
        categories = todo_data.get("categories", "")
        if categories:
            todo.add("categories", categories.split(","))

        # Add timestamps
        now = dt.datetime.now(dt.UTC)
        todo.add("created", now)
        todo.add("dtstamp", now)
        todo.add("last-modified", now)

        cal.add_component(todo)
        return cal.to_ical().decode("utf-8")

    def _parse_ical_todo(self, ical_text: str) -> dict[str, Any] | None:
        """Parse iCalendar text and extract todo data."""
        try:
            cal = Calendar.from_ical(ical_text)
            for component in cal.walk():
                if component.name == "VTODO":
                    todo_data = {
                        "uid": str(component.get("uid", "")),
                        "summary": str(component.get("summary", "")),
                        "description": str(component.get("description", "")),
                        "status": str(component.get("status", "NEEDS-ACTION")),
                        "priority": int(component.get("priority", 0)),
                        "percent_complete": int(component.get("percent-complete", 0)),
                    }

                    # Handle due date
                    due = component.get("due")
                    if due:
                        todo_data["due"] = due.dt.isoformat()

                    # Handle start date
                    dtstart = component.get("dtstart")
                    if dtstart:
                        todo_data["dtstart"] = dtstart.dt.isoformat()

                    # Handle completed date
                    completed = component.get("completed")
                    if completed:
                        todo_data["completed"] = completed.dt.isoformat()

                    # Handle categories
                    categories = component.get("categories")
                    if categories:
                        todo_data["categories"] = self._extract_categories(categories)

                    return todo_data

            return None

        except Exception as e:
            logger.error("Error parsing iCalendar todo: %s", e)
            return None

    def _merge_ical_todo_properties(
        self, raw_ical: str, todo_data: dict[str, Any], todo_uid: str
    ) -> str:
        """Merge new todo data into existing raw iCal while preserving all properties."""
        try:
            logger.debug(
                "Merging todo properties for %s: %s", todo_uid, list(todo_data.keys())
            )
            cal = Calendar.from_ical(raw_ical)

            for component in cal.walk():
                if component.name == "VTODO":
                    # Update only provided properties
                    if "summary" in todo_data:
                        component["SUMMARY"] = todo_data["summary"]
                    if "description" in todo_data:
                        component["DESCRIPTION"] = todo_data["description"]
                    if "status" in todo_data:
                        status_value = todo_data["status"].upper()
                        component["STATUS"] = status_value
                        logger.debug("Set STATUS to %s", status_value)
                    if "priority" in todo_data:
                        component["PRIORITY"] = todo_data["priority"]
                    if "percent_complete" in todo_data:
                        percent_value = todo_data["percent_complete"]
                        component["PERCENT-COMPLETE"] = percent_value
                        logger.debug("Set PERCENT-COMPLETE to %s", percent_value)

                    # Handle due date
                    if "due" in todo_data:
                        due_str = todo_data["due"]
                        if due_str:
                            due_dt = self._ensure_timezone_aware(due_str)
                            component["DUE"] = vDDDTypes(due_dt)
                            logger.debug("Set DUE to %s", due_dt)

                    # Handle start date
                    if "dtstart" in todo_data:
                        dtstart_str = todo_data["dtstart"]
                        if dtstart_str:
                            dtstart_dt = self._ensure_timezone_aware(dtstart_str)
                            component["DTSTART"] = vDDDTypes(dtstart_dt)
                            logger.debug("Set DTSTART to %s", dtstart_dt)

                    # Handle completed date
                    if "completed" in todo_data:
                        completed_str = todo_data["completed"]
                        if completed_str:
                            completed_dt = self._ensure_timezone_aware(completed_str)
                            component["COMPLETED"] = vDDDTypes(completed_dt)
                            logger.debug("Set COMPLETED to %s", completed_dt)

                    # Handle categories
                    if "categories" in todo_data:
                        categories_str = todo_data["categories"]
                        if categories_str:
                            component["CATEGORIES"] = [
                                c.strip() for c in categories_str.split(",")
                            ]
                            logger.debug("Set CATEGORIES to %s", categories_str)

                    # Update timestamps
                    now = dt.datetime.now(dt.UTC)
                    component["LAST-MODIFIED"] = vDDDTypes(now)
                    component["DTSTAMP"] = vDDDTypes(now)

                    break

            return cal.to_ical().decode("utf-8")

        except Exception as e:
            logger.error("Error merging iCal todo properties: %s", e, exc_info=True)
            return self._create_ical_todo(todo_data, todo_uid)

    # ============= Helper Methods - Filtering =============

    def _extract_categories(self, categories_obj) -> str:
        """Extract categories from icalendar object to string."""
        if not categories_obj:
            return ""

        try:
            if hasattr(categories_obj, "cats"):
                # Handle Categories object with cats attribute
                return ", ".join(str(cat) for cat in categories_obj.cats)
            elif hasattr(categories_obj, "__iter__") and not isinstance(
                categories_obj, str
            ):
                # Handle list of vCategory objects or strings
                result = []
                for cat in categories_obj:
                    # Try to extract value from vCategory objects using to_ical()
                    if hasattr(cat, "to_ical"):
                        result.append(cat.to_ical().decode("utf-8"))
                    else:
                        result.append(str(cat))
                return ", ".join(result)
            else:
                # Handle single category string or object
                if hasattr(categories_obj, "to_ical"):
                    return categories_obj.to_ical().decode("utf-8")
                return str(categories_obj)
        except Exception as e:
            logger.warning("Error extracting categories: %s", e)
            return str(categories_obj)

    def _apply_event_filters(
        self, events: list[dict[str, Any]], filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Apply advanced filters to event list."""
        return [
            event for event in events if self._event_matches_filters(event, filters)
        ]

    def _event_matches_filters(
        self, event: dict[str, Any], filters: dict[str, Any]
    ) -> bool:
        """Check if an event matches the provided filters."""
        try:
            # Filter by minimum attendees
            if "min_attendees" in filters:
                attendees = event.get("attendees", "")
                attendee_count = len(attendees.split(",")) if attendees else 0
                if attendee_count < filters["min_attendees"]:
                    return False

            # Filter by categories
            if "categories" in filters:
                event_categories = event.get("categories", "").lower()
                required_categories = [cat.lower() for cat in filters["categories"]]
                if not any(cat in event_categories for cat in required_categories):
                    return False

            # Filter by status
            if "status" in filters:
                if event.get("status", "").upper() != filters["status"].upper():
                    return False

            # Filter by title contains
            if "title_contains" in filters:
                title = event.get("title", "").lower()
                search_term = filters["title_contains"].lower()
                if search_term not in title:
                    return False

            # Filter by location contains
            if "location_contains" in filters:
                location = event.get("location", "").lower()
                search_term = filters["location_contains"].lower()
                if search_term not in location:
                    return False

            return True

        except Exception:
            return True

    def _todo_matches_filters(
        self, todo: dict[str, Any], filters: dict[str, Any]
    ) -> bool:
        """Check if a todo matches the provided filters."""
        try:
            # Filter by status
            if "status" in filters:
                if todo.get("status", "").upper() != filters["status"].upper():
                    return False

            # Filter by minimum priority
            if "min_priority" in filters:
                priority = todo.get("priority", 0)
                if priority == 0 or priority > filters["min_priority"]:
                    return False

            # Filter by categories
            if "categories" in filters:
                todo_categories = todo.get("categories", "").lower()
                required_categories = [cat.lower() for cat in filters["categories"]]
                if not any(cat in todo_categories for cat in required_categories):
                    return False

            # Filter by summary contains
            if "summary_contains" in filters:
                summary = todo.get("summary", "").lower()
                search_term = filters["summary_contains"].lower()
                if search_term not in summary:
                    return False

            return True

        except Exception:
            return True

    # ============= Legacy Methods (for backward compatibility) =============

    async def bulk_update_events(
        self, filter_criteria: dict[str, Any], update_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Bulk update events matching filter criteria."""
        await self._ensure_calendar_home()
        try:
            start_datetime = None
            end_datetime = None
            if "start_date" in filter_criteria and filter_criteria["start_date"]:
                start_datetime = dt.datetime.fromisoformat(
                    filter_criteria["start_date"]
                )
            if "end_date" in filter_criteria and filter_criteria["end_date"]:
                end_datetime = dt.datetime.fromisoformat(filter_criteria["end_date"])

            events = await self.search_events_across_calendars(
                start_datetime=start_datetime,
                end_datetime=end_datetime,
                filters=filter_criteria,
            )

            updated_count = 0
            failed_count = 0
            results = []

            for event in events:
                try:
                    await self.update_event(
                        event["calendar_name"], event["uid"], update_data
                    )
                    updated_count += 1
                    results.append(
                        {
                            "uid": event["uid"],
                            "status": "updated",
                            "title": event.get("title", ""),
                        }
                    )
                except Exception as e:
                    failed_count += 1
                    results.append(
                        {
                            "uid": event["uid"],
                            "status": "failed",
                            "error": str(e),
                            "title": event.get("title", ""),
                        }
                    )

            return {
                "total_found": len(events),
                "updated_count": updated_count,
                "failed_count": failed_count,
                "results": results,
            }

        except Exception as e:
            logger.error("Error in bulk update: %s", e)
            raise

    async def find_availability(
        self,
        duration_minutes: int,
        attendees: list[str] | None = None,
        start_datetime: dt.datetime | None = None,
        end_datetime: dt.datetime | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Find available time slots for scheduling.

        Note: This is a simplified stub that returns empty list.
        Full implementation would require complex free/busy analysis.
        """
        logger.warning("find_availability is not fully implemented with AsyncDavClient")
        return []
