"""CalDAV client for Nextcloud calendar and task operations using caldav library."""

import datetime as dt
import inspect
import logging
import re
import uuid
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
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
from lxml import etree  # type: ignore[import-untyped]  # ty: ignore[unresolved-import]

from ..config import get_nextcloud_ssl_verify
from .dav_errors import DavPreconditionFailed, dav_error_from_response

logger = logging.getLogger(__name__)

# How many calendars to query at once when searching across all of them.
# Unbounded fan-out would open one connection per calendar; accounts with
# dozens of calendars would hammer the Nextcloud instance for no gain, since
# the win is already had after a handful run in parallel.
CALENDAR_FANOUT = 8


async def _maybe_await(result: Any) -> Any:
    """Await a result if it's a coroutine, otherwise return it directly.

    caldav v3 uses dual-mode methods that return coroutines for async clients
    but plain objects when the result is already available (e.g. load() on
    already-loaded objects).
    """
    if inspect.isawaitable(result):
        return await result
    return result


# How far back a recurring todo's unfinished backlog is searched. Bounded so an
# abandoned daily series cannot turn a single todo into thousands of expansions.
_PENDING_MAX_LOOKBACK = dt.timedelta(days=366 * 3)

# An occurrence in one of these states is finished and not part of the backlog.
_TODO_DONE_STATUSES = frozenset({"COMPLETED", "CANCELLED"})

# Fallback VALARM DESCRIPTION per component type. RFC 5545 requires the property
# on DISPLAY and EMAIL alarms, so a reminder that omits it still needs wording.
_EVENT_REMINDER_DESCRIPTION = "Event reminder"
_TODO_REMINDER_DESCRIPTION = "Todo reminder"

# The VALARM actions Nextcloud actually schedules (ReminderService::REMINDER_TYPES).
# RFC 5545 permits other IANA/X- tokens, which it stores but silently ignores.
_VALARM_ACTIONS = frozenset({"DISPLAY", "EMAIL", "AUDIO"})


def _calendar_is_transparent(response_elem: Any, ns: dict[str, str]) -> bool:
    """Read CalDAV ``schedule-calendar-transp`` from a PROPFIND response.

    Nextcloud exposes its per-calendar "never show me as busy" switch through
    this property (RFC 4791 5.2.9). The value is an *element*, not text:
    ``<c:schedule-calendar-transp><c:transparent/></c:schedule-calendar-transp>``.
    A missing property means opaque, i.e. the calendar consumes time.
    """
    elem = response_elem.find(".//c:schedule-calendar-transp", ns)
    return elem is not None and elem.find("./c:transparent", ns) is not None


def _rrule_to_string(rrule: Any) -> str:
    """Render an icalendar ``vRecur`` as an RFC 5545 RRULE value.

    ``str(vRecur)`` yields a Python repr (``vRecur({'FREQ': ['YEARLY']})``),
    which is not a valid RRULE and cannot be fed back into
    ``vRecur.from_ical()`` when a caller round-trips the value into an update.
    """
    if rrule is None:
        return ""
    if isinstance(rrule, list):
        if not rrule:
            return ""
        rrule = rrule[0]
    try:
        return rrule.to_ical().decode("utf-8")
    except AttributeError:
        return str(rrule)


def _shorthand_would_lose(
    stored: list[dict[str, Any]],
    default_description: str,
    default_summary: str,
) -> bool:
    """True when the stored alarms carry shape the shorthand cannot express.

    ``reminder_minutes``/``reminder_email`` rebuild to at most one DISPLAY plus
    one EMAIL, sharing a single whole-minute offset and the default wording. A
    stored set outside that shape — an absolute or sub-minute trigger, several
    distinct offsets, two alarms of the same action, a ``RELATED`` qualifier, a
    custom summary or per-alarm description — cannot survive the rebuild.

    What the caller is *changing* is deliberately not part of this. A new offset,
    or dropping the EMAIL alarm via ``reminder_email=False``, is the request
    being honoured, not data being lost. Comparing each stored offset against the
    new target made this fire on the most ordinary update there is — nudging a
    reminder's offset — which would have trained the reader to ignore it.
    """
    offsets = {reminder.get("minutes_before") for reminder in stored}
    if len(offsets) > 1:
        return True

    seen_actions = [reminder.get("action") for reminder in stored]
    if len(seen_actions) != len(set(seen_actions)):
        return True

    return any(
        "minutes_before" not in reminder
        or reminder.get("action") not in ("DISPLAY", "EMAIL")
        or reminder.get("related")
        # An EMAIL alarm the shorthand itself wrote carries the component title
        # as its subject, so that value is reproducible and not a loss.
        or (reminder.get("summary") or default_summary) != default_summary
        or (reminder.get("description") or default_description) != default_description
        for reminder in stored
    )


def _warn_if_hex_color(color: str) -> None:
    """Warn when a COLOR value will not survive Nextcloud's Calendar UI.

    RFC 7986 §5.9 defines COLOR as a CSS3 colour *name*, and Nextcloud takes that
    literally: ``getHexForColorName()`` in the Calendar app is a plain
    ``css3Colors[name]`` lookup, so ``#FF0000`` resolves to null and the event
    colour is dropped on display. The property is still written — other CalDAV
    clients may honour it — but the caller deserves to know it will not show up.
    """
    if color.startswith("#"):
        logger.warning(
            "Event color %r is a hex value; Nextcloud's Calendar UI only renders "
            "CSS3 colour names (e.g. 'tomato') and will ignore it",
            color,
        )


def _rrule_with_until(
    rrule_str: str, end_date: str, dtstart: Any, *, replace: bool = False
) -> str:
    """Return ``rrule_str`` with an ``UNTIL`` derived from ``end_date``.

    RFC 5545 §3.3.10 ties UNTIL's value type to DTSTART's: a DATE-valued DTSTART
    (an all-day event) takes a DATE, and anything else takes a UTC date-time —
    including a TZID-bound DTSTART, whose UNTIL must still be expressed in UTC.
    Getting this wrong is not cosmetic; clients drop a recurrence set whose UNTIL
    does not match, so the series silently never ends.

    ``end_date`` is inclusive-by-day: a date-only value becomes the last moment of
    that day (23:59:59 UTC) for a timed event, so "recur until June 30th" keeps the
    occurrence *on* June 30th rather than dropping it.

    An existing UNTIL or COUNT raises ``ValueError`` — they are mutually exclusive
    with UNTIL in one rule, and quietly choosing a winner is the silent-ignore
    behaviour this exists to remove. ``replace=True`` drops them instead, for the
    update path: applying an end date to the *stored* rule of an already-bounded
    series means "move the end", not "contradict yourself".
    """
    parts = _rrule_parts_without_bounds(rrule_str, replace=replace)
    all_day = isinstance(dtstart, dt.date) and not isinstance(dtstart, dt.datetime)
    # DTSTART's zone is what "that day" means to the caller, so an end-of-day
    # boundary has to be built in it before being converted to UTC.
    tz = None if all_day else getattr(dtstart, "tzinfo", None)
    until = _format_until(end_date, all_day=all_day, tz=tz)
    return ";".join([*parts, f"UNTIL={until}"])


def _rrule_parts_without_bounds(rrule_str: str, *, replace: bool) -> list[str]:
    """Split an RRULE into parts, dropping or rejecting UNTIL/COUNT."""
    parts = []
    for part in rrule_str.split(";"):
        if not part:
            continue
        name = part.split("=", 1)[0].strip().upper()
        if name not in ("UNTIL", "COUNT"):
            parts.append(part)
        elif not replace:
            raise ValueError(
                f"recurrence_end_date conflicts with {name} already present in "
                f"recurrence_rule ({rrule_str!r}); pass one or the other"
            )
    return parts


def _format_until(end_date: str, *, all_day: bool, tz: dt.tzinfo | None = None) -> str:
    """Render ``end_date`` as an UNTIL value of the type DTSTART requires.

    ``tz`` is DTSTART's own zone. RFC 5545 requires UNTIL to be *formatted* in
    UTC for a date-time DTSTART, but that is a serialisation rule, not an
    instruction to anchor the boundary to UTC midnight: "until June 30th" means
    the end of June 30th where the event happens. Anchoring in UTC drops the
    last occurrence of any evening event in a zone behind UTC, because its real
    instant falls on the following UTC date — 21:00 in New York on the 30th is
    01:00 UTC on the 1st, past a ``T235959Z`` cutoff.
    """
    try:
        parsed = dt.datetime.fromisoformat(end_date)
    except ValueError:
        raise ValueError(
            f"recurrence_end_date {end_date!r} is not an ISO 8601 date or datetime"
        ) from None

    if all_day:
        if end_date != end_date.split("T")[0]:
            # RFC 5545 ties UNTIL's value type to DTSTART's, so an all-day series
            # can only be bounded by a DATE. Dropping the time is the sole
            # correct reading rather than a caller error, hence debug not warn —
            # but it should still be visible to anyone wondering where it went.
            logger.debug(
                "recurrence_end_date %r carries a time of day, which an all-day "
                "series cannot express in UNTIL; using the date alone",
                end_date,
            )
        return parsed.date().strftime("%Y%m%d")

    if end_date == end_date.split("T")[0]:
        # Date-only input: bound the whole day rather than midnight, which would
        # exclude an occurrence happening later on that date.
        parsed = parsed.replace(hour=23, minute=59, second=59)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz or dt.UTC)
    return parsed.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _occurrence_is_done(component: Any) -> bool:
    """True when a VTODO occurrence is finished.

    Clients that materialise recurrences (jtx Board via DAVx5, for one) mark a
    completed instance by writing an override with ``STATUS:COMPLETED``. Some
    leave ``STATUS`` alone and only set ``PERCENT-COMPLETE:100``, so both are
    treated as done.
    """
    status = str(component.get("status") or "").upper()
    if status in _TODO_DONE_STATUSES:
        return True
    try:
        return int(component.get("percent-complete", 0)) >= 100
    except (TypeError, ValueError):
        return False


def _as_utc_datetime(value: dt.date) -> dt.datetime:
    """Normalize a date or datetime to an aware UTC datetime for ordering."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)


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
            ssl_verify_cert=get_nextcloud_ssl_verify(),  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # caldav types say bool|str but passes through to niquests which accepts SSLContext
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
            # calendar-home-set returns an absolute path that already includes
            # any subpath under which Nextcloud is served (e.g.
            # ``/nextcloud/remote.php/dav/calendars/David/``). Resolve it
            # against the *origin* (scheme + host) of ``base_url`` rather than
            # the full ``base_url`` — concatenating onto a subpath base URL
            # would double the subpath and produce a bogus, unroutable URL
            # (issue #1007).
            origin = urlsplit(self.base_url)
            home_url = urlunsplit((origin.scheme, origin.netloc, home_url, "", ""))
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
            client=self._dav_client,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]  # AsyncDAVClient is valid for async mode
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
        items_found = await calendar.search(  # type: ignore[misc]  # ty: ignore[invalid-await]  # dual-mode: returns coroutine for async clients
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
        <c:schedule-calendar-transp/>
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

            transparent = _calendar_is_transparent(response_elem, ns)

            result.append(
                {
                    "name": calendar_name,
                    "display_name": display_name,
                    "description": description,
                    "color": color,
                    "href": calendar_url,
                    "read_only": is_subscribed,
                    "source": source,
                    "transparent": transparent,
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
            # The REPORT above asks for getetag, so the objects carry their own.
            fallback_etags: dict[str, str] = {}
        else:
            events = await calendar.events()  # type: ignore[misc]  # ty: ignore[invalid-await]  # dual-mode
            do_expand = False
            # caldav's events() asks for calendar-data only (verified: .etag is
            # None on every object it returns), so without this the unfiltered
            # listing would pay one PROPFIND per event -- the same 1 + N the
            # date-range path and list_todos were fixed for. One batched
            # collection PROPFIND covers the whole result set.
            fallback_etags = await self._collection_etags(calendar)

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
            # One ETag per stored object: expanded recurrence instances are
            # views of the same resource, so a conditional write against any of
            # them targets that one object. Both sources are per-listing, never
            # per-event -- see the branch above.
            etag = event.etag or fallback_etags.get(unquote(urlsplit(href).path), "")
            event_dicts = self._expand_event_occurrences(
                cal, start_datetime, end_datetime, do_expand
            )
            for event_dict in event_dicts:
                event_dict["href"] = href
                event_dict["etag"] = etag
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

        if start_datetime is None or end_datetime is None:
            # Expansion needs a window. Checked before the try rather than
            # asserted inside it, where the except would have reported a
            # caller error as a failed expansion (python:S5779).
            logger.warning(
                "Recurrence expansion requested without a date window; "
                "returning master event"
            )
            return [
                self._extract_vevent_data(component) for component in cal.walk("VEVENT")
            ]

        try:
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

        # Ask for the ETag alongside the calendar data. The REPORT is the hot
        # path for "list events in a range", and objects built from it are
        # already loaded -- so without getetag here, surfacing an ETag per event
        # would fall back to one PROPFIND *each*, turning a single REPORT into
        # 1 + N requests.
        data = cdav.CalendarData()
        query = (
            cdav.CalendarQuery() + [dav.Prop() + data + dav.GetEtag()] + filter_element
        )

        body = etree.tostring(
            query.xmlelement(), encoding="utf-8", xml_declaration=True
        )
        assert calendar.client is not None
        response = await calendar.client.report(str(calendar.url), body, depth=1)  # type: ignore[misc]  # ty: ignore[invalid-await]  # dual-mode

        # Parse response (same pattern as AsyncCalendar.search)
        objects = []
        response_data = response.expand_simple_props(
            [cdav.CalendarData(), dav.GetEtag()]
        )
        for href, props in response_data.items():
            if href == str(calendar.url):
                continue
            cal_data = props.get(cdav.CalendarData.tag)
            if cal_data:
                etag = props.get(dav.GetEtag.tag)
                obj = AsyncEvent(
                    client=calendar.client,
                    url=calendar.url.join(href),  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]  # url is always set for calendars
                    data=cal_data,
                    parent=calendar,
                    # caldav's ``etag`` property reads straight from ``props``,
                    # so seeding it here is what keeps _object_etag off the
                    # PROPFIND fallback.
                    props={dav.GetEtag.tag: etag} if etag else None,
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
        event = await calendar.save_event(ical=ical_content)  # type: ignore[misc]  # ty: ignore[invalid-await]  # dual-mode

        logger.debug("Created event %s", event_uid)

        return {
            "uid": event_uid,
            "href": str(event.url),
            "etag": await self._object_etag(event),
            "status_code": 201,
        }

    async def update_event(
        self,
        calendar_name: str,
        event_uid: str,
        event_data: dict[str, Any],
        etag: str = "",
    ) -> dict[str, Any]:
        """Update an existing calendar event.

        Args:
            etag: The ETag the caller last read. The write is refused with
                :class:`DavPreconditionFailed` if the event changed since. When
                omitted, the ETag observed during this call's own read is used,
                which still closes the read-modify-write window opened here.

        Raises:
            DavPreconditionFailed: If the event changed since ``etag`` was read.
        """
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        # Find the event by UID using caldav library
        event = await self._async_object_by_uid(
            calendar, event_uid, cdav.CompFilter("VEVENT")
        )
        await _maybe_await(event.load(only_if_unloaded=True))

        # The caller's ETag wins: it is their concurrency assertion, covering
        # the whole read-modify-write cycle rather than just our own read.
        # Matches the rule the contacts client already follows.
        if not etag:
            etag = await self._object_etag(event)

        # Merge updates into existing iCal data
        updated_ical = self._merge_ical_properties(event.data, event_data)  # type: ignore[arg-type]

        new_etag = await self._conditional_put(
            event, updated_ical, etag, kind="event", uid=event_uid
        )

        logger.debug("Updated event %s", event_uid)
        return {
            "uid": event_uid,
            "href": str(event.url),
            "etag": new_etag,
            "status_code": 200,
        }

    @staticmethod
    def _status_from_dav_error(exc: caldav_error.DAVError) -> int | None:
        """Best-effort HTTP status from a caldav DAVError, or ``None``.

        caldav offers nothing structured here. ``_post_delete`` raises
        ``DeleteError(errmsg(r))``, and ``errmsg`` formats
        ``"<status> <reason>\\n\\n<body>"`` — which lands in the exception's
        ``url`` slot, because ``DAVError.__init__``'s first positional parameter
        is ``url``. So the status is the leading integer of ``exc.url``, with
        ``str(exc)`` as a fallback in case a future caldav populates it properly.

        Returns ``None`` when nothing parses; callers must treat that as an
        unknown refusal rather than substituting a plausible-looking code.
        """
        for candidate in (getattr(exc, "url", None), str(exc)):
            if not candidate:
                continue
            match = re.search(r"\b(\d{3})\b", str(candidate))
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    async def _object_etag(obj: Any) -> str:
        """Return *obj*'s current strong ETag, or ``""`` if the server withheld one.

        ``load()`` already populates ``{DAV:}getetag`` from the GET response
        (verified against Nextcloud 32.0.14), so the common path costs no extra
        request. The PROPFIND is the fallback for an object reached without a
        load.
        """
        etag = obj.etag
        if etag:
            return etag

        try:
            props = await obj.get_properties([dav.GetEtag()])
        except Exception as e:
            # An ETag is an optimisation for the caller, not a precondition for
            # returning the object they asked for. Failing the read because the
            # concurrency token could not be fetched would be a worse trade.
            logger.debug("Could not fetch etag for %s: %s", obj.url, e)
            return ""

        return props.get(dav.GetEtag.tag) or ""

    async def _collection_etags(self, calendar: Any) -> dict[str, str]:
        """Map decoded href path -> ETag for every object in *calendar*.

        One ``PROPFIND`` (Depth 1, ``getetag`` only) instead of one per object.
        Mirrors the lightweight ctag/etag PROPFIND the contacts client already
        uses. Returns ``{}`` on failure -- an ETag is a token for the caller,
        not a reason to fail the listing that produced it.
        """
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>'
        )

        try:
            response = await self._dav_client.request(
                str(calendar.url), "PROPFIND", body, {"Depth": "1"}
            )
            # Depth 1 includes the collection's own href. Unlike the REPORT
            # parser, this does not skip it: nothing ever looks the collection
            # up (only object hrefs are queried), and a collection ctag in the
            # map is inert. Left in rather than filtered so this stays a plain
            # transcription of the response.
            return {
                unquote(urlsplit(href).path): etag
                for href, props in response.expand_simple_props([dav.GetEtag()]).items()
                if (etag := props.get(dav.GetEtag.tag))
            }
        except Exception as e:
            logger.debug("Collection etag PROPFIND failed for %s: %s", calendar.url, e)
            return {}

    async def _conditional_put(
        self, obj: Any, ical: str, etag: str, *, kind: str, uid: str
    ) -> str:
        """PUT *ical* over *obj*, guarded by ``If-Match``. Returns the new ETag.

        ``caldav``'s own ``save()`` sends no ``If-Match``, so two concurrent
        updates silently last-write-win. It also *returns* a 412 rather than
        raising, so the status is checked here explicitly.

        Raises:
            DavPreconditionFailed: If the object changed since *etag* was read.
            HTTPStatusError: For any other refusal.
        """
        url = str(obj.url)
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag

        response = await self._dav_client.put(url, ical, headers)
        status = response.status

        if status >= 400:
            logger.warning(
                "Conditional PUT of %s %s refused with %s", kind, uid, status
            )
            raise dav_error_from_response(
                status, method="PUT", url=url, body=getattr(response, "raw", b"") or b""
            )

        # Nextcloud returns the new ETag on the write, so the caller can chain
        # another conditional update without re-reading.
        return response.headers.get("etag", "")

    async def _delete_dav_object(
        self,
        calendar_name: str,
        uid: str,
        comp_filter: Any,
        kind: str,
    ) -> dict[str, Any]:
        """Delete one CalDAV object, mapping refusals to a structured result.

        Only ``NotFoundError`` used to be caught, so a 403 (Nextcloud refuses to
        delete iMIP/scheduled objects) or a 409/412 (a stale entry in the calendar
        trashbin colliding with the delete) escaped as a raw caldav traceback out
        of the MCP tool.

        Only ``DeleteError`` is treated as a refusal: it is precisely what
        ``DAVObject._post_delete`` raises when the server rejects the DELETE.
        caldav's other error classes are flat siblings under ``DAVError``, not
        subclasses of it, so ``AuthorizationError`` (expired credential) and
        ``RateLimitError`` (retryable) keep propagating instead of being flattened
        into a per-object "the server refused this event" message. Catching the
        ``DAVError`` base would have masked exactly those.
        """
        await self._ensure_calendar_home()
        calendar = self._get_calendar(calendar_name)

        try:
            obj = await self._async_object_by_uid(calendar, uid, comp_filter)
            await _maybe_await(obj.delete())
            logger.debug("Deleted %s %s", kind, uid)
            return {"success": True, "status_code": 204}
        except caldav_error.NotFoundError as e:
            logger.debug("%s %s not found: %s", kind.capitalize(), uid, e)
            return {"success": True, "status_code": 404}
        except caldav_error.DeleteError as e:
            status = self._status_from_dav_error(e)
            logger.warning(
                "Server refused to delete %s %s (status %s)",
                kind,
                uid,
                status if status is not None else "unknown",
            )
            return {
                "success": False,
                "status_code": status if status is not None else 500,
                "message": self._delete_refusal_message(status, kind),
                "reason": str(e),
            }

    @staticmethod
    def _delete_refusal_message(status: int | None, kind: str) -> str:
        """Explain a delete refusal in terms of its likely cause."""
        if status == 403:
            return (
                f"The server refused to delete this {kind}. Nextcloud rejects "
                "deletion of scheduled (iMIP) objects — if you are an attendee, "
                "decline the invitation instead; if you are the organizer, cancel "
                "it so attendees are notified."
            )
        if status in (409, 412, 500):
            return (
                f"The server refused to delete this {kind}, most likely because a "
                "previously-deleted object with the same UID is still in the "
                "calendar trashbin. Empty the trashbin in the Nextcloud Calendar "
                "UI and retry."
            )
        return f"The server refused to delete this {kind}" + (
            f" (HTTP {status})." if status is not None else "."
        )

    async def delete_event(self, calendar_name: str, event_uid: str) -> dict[str, Any]:
        """Delete a calendar event.

        Returns a structured result rather than raising on a server refusal —
        see :meth:`_delete_dav_object`.
        """
        return await self._delete_dav_object(
            calendar_name, event_uid, cdav.CompFilter("VEVENT"), "event"
        )

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

        etag = await self._object_etag(event)
        event_data["href"] = str(event.url)
        event_data["etag"] = etag

        logger.debug("Retrieved event %s", event_uid)
        return event_data, etag

    async def _events_from_calendar(
        self,
        calendar: dict[str, Any],
        start_datetime: dt.datetime | None,
        end_datetime: dt.datetime | None,
        filters: dict[str, Any] | None,
        limiter: anyio.CapacityLimiter,
        slots: list[list[dict[str, Any]]],
        index: int,
    ) -> None:
        """One calendar's events, annotated, written into its own slot.

        Failure is contained per calendar: a single unreachable or broken
        calendar is logged and skipped, exactly as in the previous serial
        loop, rather than cancelling the whole task group.
        """
        async with limiter:
            try:
                events = await self.get_calendar_events(
                    calendar["name"], start_datetime, end_datetime
                )
            except Exception as e:
                logger.warning(
                    "Error getting events from calendar %s: %s", calendar["name"], e
                )
                return

        # Apply filters if provided
        if filters:
            events = self._apply_event_filters(events, filters)

        # Add calendar info to each event
        for event in events:
            event["calendar_name"] = calendar["name"]
            event["calendar_display_name"] = calendar.get(
                "display_name", calendar["name"]
            )

        slots[index] = events

    async def search_events_across_calendars(
        self,
        start_datetime: dt.datetime | None = None,
        end_datetime: dt.datetime | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search events across all calendars with advanced filtering.

        The per-calendar REPORTs are fanned out via ``anyio.create_task_group``
        instead of awaited one after another. The serial loop cost one full
        round trip per calendar, so the runtime grew linearly with the number
        of calendars while barely depending on how many events came back --
        measured against a real account with 21 calendars, a one-day window
        took 2.73 s and a 180-day window 2.93 s. The time was round trips, not
        data.

        Concurrency is bounded by ``CALENDAR_FANOUT``: an account with a
        hundred calendars should not open a hundred simultaneous connections
        to the Nextcloud instance.

        Results are written into pre-allocated slots rather than appended, so
        the returned order still follows the calendar order and does not
        depend on which REPORT happens to finish first.
        """
        await self._ensure_calendar_home()
        try:
            calendars = await self.list_calendars()
            slots: list[list[dict[str, Any]]] = [[] for _ in calendars]
            limiter = anyio.CapacityLimiter(CALENDAR_FANOUT)

            async with anyio.create_task_group() as tg:
                for index, calendar in enumerate(calendars):
                    tg.start_soon(
                        self._events_from_calendar,
                        calendar,
                        start_datetime,
                        end_datetime,
                        filters,
                        limiter,
                        slots,
                        index,
                    )

            return [event for events in slots for event in events]

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
        todos = await calendar.todos(include_completed=True)  # type: ignore[misc]  # ty: ignore[invalid-await]  # dual-mode

        # caldav's todos() REPORT asks for calendar-data only, so none of these
        # objects carry an etag and _object_etag would PROPFIND once per todo --
        # measured at 1 + N against a live instance. Its ``props`` argument
        # cannot carry getetag (it expects calendar-data-shaped elements only),
        # so fetch them all in a single collection PROPFIND instead: 1 + 1.
        # Skipped entirely for an empty calendar -- there is nothing to key.
        etags = await self._collection_etags(calendar) if todos else {}

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
                href = str(todo.url)
                todo_dict["href"] = href
                todo_dict["etag"] = etags.get(unquote(urlsplit(href).path), "")

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
        todo = await calendar.save_todo(ical=ical_content)  # type: ignore[misc]  # ty: ignore[invalid-await]  # dual-mode

        logger.debug("Created todo %s", todo_uid)

        return {
            "uid": todo_uid,
            "href": str(todo.url),
            "etag": await self._object_etag(todo),
            "status_code": 201,
        }

    async def update_todo(
        self,
        calendar_name: str,
        todo_uid: str,
        todo_data: dict[str, Any],
        etag: str = "",
    ) -> dict[str, Any]:
        """Update an existing todo/task.

        Args:
            etag: The ETag the caller last read. The write is refused with
                :class:`DavPreconditionFailed` if the todo changed since. When
                omitted, the ETag observed during this call's own read is used.

        Raises:
            DavPreconditionFailed: If the todo changed since ``etag`` was read.
        """
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

            # The caller's ETag wins -- see update_event for the reasoning.
            if not etag:
                etag = await self._object_etag(todo)

            # Merge updates into existing iCal data
            updated_ical = self._merge_ical_todo_properties(
                todo.data,  # type: ignore[arg-type]
                todo_data,
                todo_uid,
            )
            logger.debug("Merged iCal data length: %s", len(updated_ical))

            new_etag = await self._conditional_put(
                todo, updated_ical, etag, kind="todo", uid=todo_uid
            )

            logger.debug("Updated todo %s", todo_uid)
            return {
                "uid": todo_uid,
                "href": str(todo.url),
                "etag": new_etag,
                "status_code": 200,
            }
        except DavPreconditionFailed:
            # Listed before the catch-all because it is not a failure of this
            # call: it is the If-Match guard doing its job when another writer
            # got there first. The tool layer already logs it at debug and
            # translates it into an actionable error, so logging it as an error
            # here would make every contended write look like a server fault --
            # in exactly the concurrent-write scenario someone would be reading
            # these logs to understand.
            raise
        except Exception as e:
            logger.error("Error updating todo %s: %s", todo_uid, e)
            raise

    async def delete_todo(self, calendar_name: str, todo_uid: str) -> dict[str, Any]:
        """Delete a todo/task.

        Returns a structured result rather than raising on a server refusal —
        see :meth:`_delete_dav_object`.
        """
        return await self._delete_dav_object(
            calendar_name, todo_uid, cdav.CompFilter("VTODO"), "todo"
        )

    async def _todos_from_calendar(
        self,
        calendar: dict[str, Any],
        filters: dict[str, Any] | None,
        limiter: anyio.CapacityLimiter,
        slots: list[list[dict[str, Any]]],
        index: int,
    ) -> None:
        """One calendar's todos, annotated. Failure is contained per calendar."""
        async with limiter:
            try:
                todos = await self.list_todos(calendar["name"], filters)
            except Exception as e:
                logger.warning(
                    "Error getting todos from calendar %s: %s", calendar["name"], e
                )
                return

        # Add calendar info to each todo
        for todo in todos:
            todo["calendar_name"] = calendar["name"]
            todo["calendar_display_name"] = calendar.get(
                "display_name", calendar["name"]
            )

        slots[index] = todos

    async def search_todos_across_calendars(
        self, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Search todos across all calendars.

        Fanned out like :meth:`search_events_across_calendars` - same serial
        round-trip problem, same bounded task group, same stable ordering.
        """
        await self._ensure_calendar_home()
        try:
            calendars = await self.list_calendars()
            slots: list[list[dict[str, Any]]] = [[] for _ in calendars]
            limiter = anyio.CapacityLimiter(CALENDAR_FANOUT)

            async with anyio.create_task_group() as tg:
                for index, calendar in enumerate(calendars):
                    tg.start_soon(
                        self._todos_from_calendar,
                        calendar,
                        filters,
                        limiter,
                        slots,
                        index,
                    )

            return [todo for todos in slots for todo in todos]

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

    @staticmethod
    def _stored_is_all_day(component, prop: str = "DTSTART") -> bool | None:
        """Whether ``prop`` on the stored component is a DATE (all-day) value.

        Returns ``None`` when the property is absent, so callers can distinguish
        "unknown" from "timed" — the update path needs that difference to decide
        whether to inherit the stored value type or fall back to a default.
        """
        value = component.get(prop)
        if value is None:
            return None
        inner = getattr(value, "dt", None)
        if inner is None:
            return None
        return isinstance(inner, dt.date) and not isinstance(inner, dt.datetime)

    @staticmethod
    def _stored_tzid(component, prop: str = "DTSTART") -> str | None:
        """Return the TZID parameter on ``prop``, if the stored value carries one.

        ``None`` for all-day, floating and UTC values — none of which should have
        a zone inherited onto them.
        """
        value = component.get(prop)
        if value is None:
            return None
        tzid = getattr(value, "params", {}).get("TZID")
        return str(tzid) if tzid else None

    @classmethod
    def _parse_event_datetime(
        cls,
        dt_str: str,
        tz_name: str | None = None,
        *,
        inherited_tz: str | None = None,
    ) -> tuple[dt.datetime, ZoneInfo | None]:
        """Parse an ISO datetime string with optional TZID application.

        Returns ``(parsed_dt, applied_zoneinfo)`` where ``applied_zoneinfo``
        is non-None only when a zone was applied to a naive input — the
        caller uses this to know whether to emit a VTIMEZONE component.

        ``tz_name`` is the caller's explicit request; ``inherited_tz`` is the TZID
        already on the stored property. They are separate parameters on purpose:
        passing the stored zone as ``tz_name`` would fire the "explicit offset;
        ignoring timezone" warning spuriously on every offset-bearing update, and
        would override a caller who deliberately wants floating time. Explicit
        always wins over inherited.
        """
        parsed = dt.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        zi = cls._resolve_timezone(tz_name) if tz_name else None

        if parsed.tzinfo is not None:
            # Only complain about a zone the caller actually asked for.
            if tz_name:
                logger.warning(
                    "Datetime %r has an explicit offset; ignoring timezone=%r",
                    dt_str,
                    tz_name,
                )
            return parsed, None

        if zi is not None:
            return parsed.replace(tzinfo=zi), zi

        if inherited_tz:
            # Resolve quietly. A stored TZID is not guaranteed to be an IANA name:
            # icalendar renders a fixed-offset tzinfo as TZID="UTC-04:00" with no
            # VTIMEZONE, so inheriting one is expected to fail. Routing that
            # through _resolve_timezone would log "Unknown IANA timezone", which
            # reads as an error when the floating-time fallback below is the
            # correct, harmless outcome. An explicit `timezone=` from the caller
            # still warns — that one really is a mistake worth surfacing.
            try:
                inherited_zi = ZoneInfo(inherited_tz)
            except (ZoneInfoNotFoundError, ValueError):
                logger.debug(
                    "Stored TZID %r is not an IANA name; not inheriting it",
                    inherited_tz,
                )
                inherited_zi = None
            if inherited_zi is not None:
                # Inheriting is the expected behaviour, not a problem — debug, not
                # warning. Without this, updating the time of a TZID-bound event
                # without re-passing `timezone` silently produced floating time.
                logger.debug(
                    "Datetime %r is naive; inheriting stored TZID=%r",
                    dt_str,
                    inherited_tz,
                )
                return parsed.replace(tzinfo=inherited_zi), inherited_zi

        logger.warning(
            "Datetime %r is naive and no timezone was supplied — storing as RFC 5545 floating local time",
            dt_str,
        )
        return parsed, None

    @staticmethod
    def _build_valarm(
        reminder: dict[str, Any],
        default_summary: str,
        default_description: str = _EVENT_REMINDER_DESCRIPTION,
    ) -> Alarm:
        """Build one VALARM from a ``Reminder``-shaped dict.

        Trigger precedence is ``trigger_at`` > ``trigger`` > ``minutes_before``;
        the model guarantees exactly one is set, so the order only decides which
        wins if a caller reaches this helper directly.

        Two RFC 5545 rules are enforced here rather than left to the server:

        * ``RELATED`` qualifies a *duration* trigger. Attaching it to an absolute
          one produces ``TRIGGER;RELATED=START;VALUE=DATE-TIME:...``, which is
          invalid, so it is only emitted on the relative branches.
        * An ``EMAIL`` alarm must carry ``SUMMARY`` (the subject) alongside
          ``DESCRIPTION`` (the body). Nextcloud addresses the message itself,
          from the component's ATTENDEEs and the calendar's sharees, so no
          alarm-level ATTENDEE is needed.
        * ``DESCRIPTION`` belongs to ``DISPLAY`` and ``EMAIL`` alarms only. The
          ``audioprop`` grammar admits ACTION, TRIGGER, DURATION+REPEAT and
          ATTACH — writing a description onto an AUDIO alarm produces a
          spec-invalid component even though lenient parsers accept it.
        """
        alarm = Alarm()
        action = reminder.get("action", "DISPLAY")
        alarm.add("action", action)
        if action in ("DISPLAY", "EMAIL"):
            alarm.add("description", reminder.get("description") or default_description)
        if action == "EMAIL":
            alarm.add("summary", reminder.get("summary") or default_summary)

        params: dict[str, str] = {}
        related = reminder.get("related")
        if related:
            params["RELATED"] = str(related)

        if reminder.get("trigger_at"):
            trigger_dt = dt.datetime.fromisoformat(str(reminder["trigger_at"]))
            if trigger_dt.tzinfo is None:
                trigger_dt = trigger_dt.replace(tzinfo=dt.UTC)
            # RELATED is deliberately dropped here: it has no meaning on an
            # absolute trigger and makes the property invalid.
            alarm.add("trigger", trigger_dt, parameters={"VALUE": "DATE-TIME"})
        elif reminder.get("trigger"):
            alarm.add(
                "trigger",
                vDDDTypes.from_ical(str(reminder["trigger"])),
                parameters=params,
            )
        elif "minutes_before" in reminder:
            minutes = int(reminder["minutes_before"])
            alarm.add("trigger", dt.timedelta(minutes=-minutes), parameters=params)
        else:
            # The Reminder model already enforces this for anything arriving via
            # an MCP tool, but the client is usable directly and a bare KeyError
            # would name a dict key rather than the thing the caller got wrong.
            raise ValueError(
                "a reminder needs one of trigger_at, trigger or minutes_before; "
                f"got {sorted(reminder)}"
            )

        return alarm

    @staticmethod
    def _extract_valarms(component: Any) -> list[dict[str, Any]]:
        """Read a component's VALARMs back into ``Reminder``-shaped dicts.

        An alarm that cannot be represented is skipped rather than surfaced
        half-built, because the caller's next step is model validation and a
        malformed alarm must cost at most itself, never the listing it is in.
        """
        reminders = []
        for sub in component.subcomponents:
            if sub.name != "VALARM":
                continue
            reminder = CalendarClient._extract_valarm(sub)
            if reminder is not None:
                reminders.append(reminder)
        return reminders

    @staticmethod
    def _extract_valarm(alarm: Any) -> dict[str, Any] | None:
        """Read one VALARM into a ``Reminder``-shaped dict, or ``None``.

        Stored data is normalised to what ``Reminder`` accepts, because this
        parses whatever any CalDAV client wrote, not only what we write. RFC 5545
        §3.8.6.1 allows ACTION to carry any IANA or ``X-`` token, so a legacy
        ``PROCEDURE`` or a custom action is realistic — and left unnormalised it
        would fail the model's ``Literal`` and take down the whole listing the
        todo appears in, rather than the one alarm. Nextcloud's own
        ``ReminderService`` discards an alarm it does not recognise instead of
        rejecting its component, so unknown actions degrade to DISPLAY here.

        ``None`` means the alarm has no usable TRIGGER. RFC 5545 makes TRIGGER
        mandatory and Nextcloud cannot schedule an alarm without one, so there is
        nothing to report — and ``Reminder`` requires exactly one trigger field,
        so returning a trigger-less dict would fail validation for the whole
        component just as an unknown ACTION would.
        """
        action = str(alarm.get("action", "DISPLAY")).upper()
        if action not in _VALARM_ACTIONS:
            logger.warning(
                "VALARM action %r is not one of %s; reporting it as DISPLAY. "
                "Nextcloud does not schedule non-standard alarms either",
                action,
                ", ".join(sorted(_VALARM_ACTIONS)),
            )
            action = "DISPLAY"

        reminder: dict[str, Any] = {
            "action": action,
            "description": str(alarm.get("description", "")),
        }
        summary = alarm.get("summary")
        if summary:
            reminder["summary"] = str(summary)

        trigger = alarm.get("trigger")
        if trigger is None or getattr(trigger, "dt", None) is None:
            logger.warning(
                "Skipping a VALARM with no usable TRIGGER; RFC 5545 requires one "
                "and Nextcloud cannot schedule the alarm without it"
            )
            return None

        value = trigger.dt
        if not isinstance(value, dt.timedelta):
            reminder["trigger_at"] = value.isoformat()
            return reminder

        # Emit exactly one trigger field, because that is what ``Reminder``
        # accepts — a dict carrying both would not validate when a caller feeds
        # a read reminder back into an update. Whole-minute offsets take the
        # friendlier ``minutes_before``, anything else keeps its raw duration.
        total = value.total_seconds()
        if total <= 0 and total % 60 == 0:
            reminder["minutes_before"] = int(-total // 60)
        else:
            reminder["trigger"] = vDDDTypes(value).to_ical().decode("utf-8")

        related = trigger.params.get("RELATED") if trigger.params else None
        if related and str(related).upper() in ("START", "END"):
            # Same reasoning as ACTION: anything else is malformed input that
            # must not propagate into the model and break the caller's listing.
            reminder["related"] = str(related).upper()
        return reminder

    @staticmethod
    def _sync_valarms(
        component: Any,
        reminders: list[dict[str, Any]],
        default_summary: str,
        default_description: str = _EVENT_REMINDER_DESCRIPTION,
    ) -> None:
        """Replace every VALARM on ``component`` with ``reminders``, in order."""
        component.subcomponents = [
            sub for sub in component.subcomponents if sub.name != "VALARM"
        ]
        for reminder in reminders:
            component.add_component(
                CalendarClient._build_valarm(
                    reminder, default_summary, default_description
                )
            )

    def _apply_reminders(
        self,
        component: Any,
        data: dict[str, Any],
        default_summary: str,
        default_description: str = _EVENT_REMINDER_DESCRIPTION,
    ) -> None:
        """Write alarms onto ``component`` from whichever form the caller used.

        An explicit ``reminders`` list is authoritative and replaces everything.
        Otherwise the older ``reminder_minutes`` / ``reminder_email`` pair is
        honoured as shorthand: a DISPLAY alarm, plus an EMAIL alarm at the same
        offset when ``reminder_email`` is set.

        Passing none of the three leaves existing alarms alone, which is what
        makes an unrelated update non-destructive.

        The two shorthand fields are independently updatable: whichever one the
        caller omits is read back off the stored alarms rather than assumed. The
        rebuild would otherwise erase what the caller never mentioned —
        ``reminder_email=True`` on its own would find no minutes, clear every
        VALARM and add nothing back, and ``reminder_minutes=45`` on its own would
        silently drop a stored EMAIL alarm.
        """
        if "reminders" in data:
            self._sync_valarms(
                component,
                data.get("reminders") or [],
                default_summary,
                default_description,
            )
            return

        if "reminder_minutes" not in data and "reminder_email" not in data:
            return

        stored = self._extract_valarms(component)
        if "reminder_minutes" in data:
            minutes = data["reminder_minutes"] or 0
        else:
            minutes = next(
                (r["minutes_before"] for r in stored if "minutes_before" in r), 0
            )
        want_email = (
            data["reminder_email"]
            if "reminder_email" in data
            else any(r["action"] == "EMAIL" for r in stored)
        )

        actions = ["DISPLAY"] + (["EMAIL"] if want_email else [])
        if _shorthand_would_lose(stored, default_description, default_summary):
            logger.warning(
                "reminder_minutes/reminder_email rebuilds the alarms as one "
                "%s at %d minutes, which does not reproduce the %d stored "
                "alarm(s); use the reminders list to edit them without loss",
                "/".join(actions),
                minutes,
                len(stored),
            )

        if minutes <= 0 and want_email:
            logger.warning(
                "reminder_email was requested but no reminder_minutes was given "
                "and none could be read from the stored alarms, so there is no "
                "offset to schedule it at"
            )

        self._sync_valarms(component, [], default_summary, default_description)
        if minutes <= 0:
            return

        for action in actions:
            component.add_component(
                self._build_valarm(
                    {
                        "action": action,
                        "description": default_description,
                        "minutes_before": minutes,
                    },
                    default_summary,
                )
            )

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

        # Kept for the RRULE below, whose UNTIL must match DTSTART's value type.
        dtstart_value: dt.date | dt.datetime | None = None

        if start_str:
            if all_day:
                start_date = dt.datetime.fromisoformat(start_str.split("T")[0]).date()
                dtstart_value = start_date
                event.add("dtstart", start_date)
                if end_str:
                    end_date = dt.datetime.fromisoformat(end_str.split("T")[0]).date()
                    event.add("dtend", end_date)
            else:
                start_dt, zi = self._parse_event_datetime(start_str, tz_name)
                if zi is not None:
                    used_timezones.add(zi)
                dtstart_value = start_dt
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

        # Add colour (RFC 7986 COLOR)
        color = event_data.get("color", "")
        if color:
            _warn_if_hex_color(color)
            event.add("color", color)

        # Handle recurrence. A non-empty rule is itself the intent to recur —
        # requiring recurring=True as well made recurrence_rule a no-op on this
        # path while the update path applied it unconditionally, so the same
        # argument meant different things depending on which tool you called.
        # ``recurring=False`` remains an explicit opt-out.
        recurrence_rule = event_data.get("recurrence_rule", "")
        if recurrence_rule and event_data.get("recurring", True):
            recurrence_end_date = event_data.get("recurrence_end_date", "")
            if recurrence_end_date:
                recurrence_rule = _rrule_with_until(
                    recurrence_rule, recurrence_end_date, dtstart_value
                )
            event.add("rrule", vRecur.from_ical(recurrence_rule))

        # Add alarms/reminders
        self._apply_reminders(event, event_data, event_data.get("title", ""))

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
            # TRANSP marks whether this event consumes time (RFC 5545
            # 3.8.2.7). Nextcloud surfaces it as Busy/Free on the event.
            # Without it a caller cannot tell that e.g. birthdays are
            # explicitly non-blocking. Absent means OPAQUE.
            "transp": str(component.get("transp", "OPAQUE")).upper(),
            "priority": int(component.get("priority", 5)),
            "privacy": str(component.get("class", "PUBLIC")),
            "url": str(component.get("url", "")),
        }

        color = component.get("color")
        if color:
            event_data["color"] = str(color)

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
            event_data["recurrence_rule"] = _rrule_to_string(rrule)
            until = (
                rrule[0].get("UNTIL") if isinstance(rrule, list) else rrule.get("UNTIL")
            )
            if until:
                # vRecur stores UNTIL as a single-element list of date/datetime.
                value = until[0] if isinstance(until, list) else until
                # Same key the update tool accepts, so a read value can be fed
                # straight back in — the write side is recurrence_end_date.
                event_data["recurrence_end_date"] = value.isoformat()

        # Handle attendees
        attendees = []
        for attendee in component.get("attendee", []):
            if isinstance(attendee, list):
                attendees.extend(str(a).replace("mailto:", "") for a in attendee)
            else:
                attendees.append(str(attendee).replace("mailto:", ""))
        if attendees:
            event_data["attendees"] = ",".join(attendees)

        reminders = self._extract_valarms(component)
        if reminders:
            event_data["reminders"] = reminders

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

    @staticmethod
    def _validate_all_day_flip(
        target_all_day: bool, has_start: bool, has_end: bool
    ) -> None:
        """Reject flips between all-day and timed that can't produce valid iCal.

        Only called when the update actually changes the value type.
        """
        if (has_start or has_end) and not (has_start and has_end):
            raise ValueError(
                "changing an event between all-day and timed requires both "
                "start_datetime and end_datetime, so DTSTART and DTEND cannot "
                "end up with mismatched value types"
            )
        if not has_start and not has_end and not target_all_day:
            raise ValueError(
                "converting an all-day event to a timed one requires "
                "start_datetime and end_datetime — there is no defensible "
                "time-of-day to invent"
            )

    def _apply_date_updates(
        self, component, event_data: dict[str, Any]
    ) -> set[ZoneInfo]:
        """Write DTSTART/DTEND onto ``component``, preserving its stored shape.

        Returns the set of zones applied, so the caller can emit the matching
        VTIMEZONE components.

        Two properties of the stored event are inherited when the caller does not
        override them, because reading them from ``event_data`` alone is what made
        updates lossy:

        * **Value type.** ``all_day`` was previously read as
          ``event_data.get("all_day", False)`` independently in each branch, so
          updating an all-day event's start without re-passing ``all_day=True``
          rewrote DTSTART as a naive DATE-TIME — RFC 5545 floating time — and could
          leave DTSTART and DTEND with mismatched value types, which is invalid
          iCalendar.
        * **TZID.** Inherited *per property*: DTSTART and DTEND may legally carry
          different zones, so sharing DTSTART's would silently relocate the end.
        """
        tz_name = event_data.get("timezone", "")
        used_timezones: set[ZoneInfo] = set()

        has_start = "start_datetime" in event_data
        has_end = "end_datetime" in event_data
        if not has_start and not has_end and "all_day" not in event_data:
            return used_timezones

        stored_all_day = self._stored_is_all_day(component, "DTSTART")
        if stored_all_day is None:
            stored_all_day = self._stored_is_all_day(component, "DTEND")

        # Computed once. Falling back to the *stored* type is the fix: absent an
        # explicit `all_day`, the event keeps the shape it already had.
        if "all_day" in event_data:
            target_all_day = bool(event_data["all_day"])
        else:
            target_all_day = bool(stored_all_day)

        # ``stored_all_day is None`` means neither DTSTART nor DTEND gave a
        # definitive type — a VEVENT with no dates at all. There is nothing to
        # flip *from*, so flip validation is deliberately skipped rather than
        # guessing; ``bool(None)`` then treats the target as timed, matching the
        # pre-existing default.
        flipping = stored_all_day is not None and target_all_day != stored_all_day

        if flipping:
            self._validate_all_day_flip(target_all_day, has_start, has_end)
            if not has_start and not has_end:
                # Timed -> all-day with no new datetimes is well defined: take the
                # dates off the stored values. (The converse already raised.)
                self._convert_component_to_all_day(component)
                return used_timezones

        if has_start:
            self._write_date_property(
                component,
                "DTSTART",
                event_data["start_datetime"],
                target_all_day,
                tz_name,
                used_timezones,
            )
        if has_end:
            self._write_date_property(
                component,
                "DTEND",
                event_data["end_datetime"],
                target_all_day,
                tz_name,
                used_timezones,
            )

        if target_all_day:
            # Apply the same zero-length guard the implicit conversion path uses.
            # An explicit ``all_day=True`` with start and end resolving to the same
            # calendar date would otherwise write ``DTEND == DTSTART`` — the very
            # zero-length DATE range this method rejects elsewhere.
            self._clamp_all_day_end(component)

        return used_timezones

    def _write_date_property(
        self,
        component,
        prop: str,
        value: str,
        all_day: bool,
        tz_name: str,
        used_timezones: set[ZoneInfo],
    ) -> None:
        """Write one DATE or DATE-TIME property, inheriting that property's TZID."""
        if all_day:
            component[prop] = vDDDTypes(
                dt.datetime.fromisoformat(value.split("T")[0]).date()
            )
            return

        parsed, zi = self._parse_event_datetime(
            value, tz_name, inherited_tz=self._stored_tzid(component, prop)
        )
        if zi is not None:
            used_timezones.add(zi)
        component[prop] = vDDDTypes(parsed)

    @staticmethod
    def _clamp_all_day_end(component) -> None:
        """Ensure an all-day ``DTEND`` is at least the day after ``DTSTART``.

        ``DTEND == DTSTART`` is a zero-length DATE range and invalid per RFC 5545.
        Shared by both routes that can produce one: the implicit timed -> all-day
        conversion (``.date()`` on a 09:00-10:00 event collapses both ends onto the
        same day) and an explicit ``all_day=True`` whose supplied start and end
        resolve to the same calendar date. A no-op unless both are DATE values.
        """
        start_value = component.get("DTSTART")
        end_value = component.get("DTEND")
        if start_value is None or end_value is None:
            return
        start_date, end_date = start_value.dt, end_value.dt
        if isinstance(start_date, dt.datetime) or isinstance(end_date, dt.datetime):
            return  # not an all-day pair; nothing to clamp
        if end_date <= start_date:
            component["DTEND"] = vDDDTypes(start_date + dt.timedelta(days=1))

    @classmethod
    def _convert_component_to_all_day(cls, component) -> None:
        """Re-write stored DTSTART/DTEND as DATE values."""
        start_value = component.get("DTSTART")
        if start_value is None:
            return
        start_date = start_value.dt
        start_date = (
            start_date.date() if isinstance(start_date, dt.datetime) else start_date
        )
        component["DTSTART"] = vDDDTypes(start_date)

        end_value = component.get("DTEND")
        if end_value is not None:
            end_date = end_value.dt
            end_date = (
                end_date.date() if isinstance(end_date, dt.datetime) else end_date
            )
            component["DTEND"] = vDDDTypes(end_date)

        cls._clamp_all_day_end(component)

    def _merge_ical_properties(self, raw_ical: str, event_data: dict[str, Any]) -> str:
        """Merge new event data into existing raw iCal while preserving all properties.

        The event's own ``UID`` is carried through from ``raw_ical`` like any other
        preserved property, so no ``event_uid`` argument is needed. (One used to be
        required solely by the removed rebuild fallback.)

        Raises on any merge failure rather than substituting a synthesised event.
        This previously caught every exception and fell back to
        ``_create_ical_event(event_data, ...)``, which rebuilds the event from the
        *partial update dict* — destroying summary, location, attendees, alarms,
        RRULE and every custom property the caller did not happen to pass, while
        reporting success.
        """
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

                # Handle colour (RFC 7986 COLOR)
                if "color" in event_data:
                    color = event_data["color"]
                    if color:
                        _warn_if_hex_color(color)
                        component["COLOR"] = color
                    elif "COLOR" in component:
                        del component["COLOR"]

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

                # Handle reminders (VALARM). Omitting all reminder arguments
                # preserves the stored alarms; ``reminders: []`` clears them.
                self._apply_reminders(
                    component,
                    event_data,
                    event_data.get("title") or str(component.get("summary", "")),
                )

                # Handle dates
                used_timezones = self._apply_date_updates(component, event_data)

                # Handle recurrence. ``recurring=False`` clears the series, which
                # previously required passing recurrence_rule="" instead — the
                # flag itself did nothing. An end date may be applied to a rule
                # supplied now or to the one already stored.
                #
                # This must run *after* _apply_date_updates: UNTIL's value type
                # follows DTSTART, so an update that flips all_day and sets
                # recurrence_end_date in one call has to see the DTSTART being
                # written, not the one being replaced. Reading the stored value
                # here would emit exactly the mismatched RRULE that makes clients
                # discard the recurrence set.
                if event_data.get("recurring") is False:
                    if event_data.get("recurrence_end_date"):
                        logger.warning(
                            "recurring=False was passed in the same update that "
                            "set recurrence_end_date, so the series is removed "
                            "and the end date has nothing to bound"
                        )
                    if event_data.get("recurrence_rule"):
                        logger.warning(
                            "recurring=False was passed in the same update that "
                            "set recurrence_rule, so the series is removed and "
                            "the new rule is discarded"
                        )
                    if "RRULE" in component:
                        del component["RRULE"]
                elif (
                    "recurrence_rule" in event_data
                    or "recurrence_end_date" in event_data
                ):
                    caller_supplied_rule = "recurrence_rule" in event_data
                    rrule_str = (
                        event_data["recurrence_rule"]
                        if caller_supplied_rule
                        else _rrule_to_string(component.get("rrule"))
                    )
                    if rrule_str:
                        end_date = event_data.get("recurrence_end_date", "")
                        if end_date:
                            dtstart = component.get("dtstart")
                            rrule_str = _rrule_with_until(
                                rrule_str,
                                end_date,
                                dtstart.dt if dtstart else None,
                                replace=not caller_supplied_rule,
                            )
                        component["RRULE"] = vRecur.from_ical(rrule_str)
                    elif "RRULE" in component:
                        if event_data.get("recurrence_end_date"):
                            logger.warning(
                                "recurrence_rule was cleared in the same update "
                                "that set recurrence_end_date, so the series is "
                                "removed and the end date has nothing to bound"
                            )
                        del component["RRULE"]
                    elif event_data.get("recurrence_end_date"):
                        # No stored rule and none supplied: there is nothing for
                        # the end date to bound, and inventing a series from an
                        # end date alone would be a guess.
                        logger.warning(
                            "recurrence_end_date was given for an event with no "
                            "recurrence rule, so it has no effect; pass "
                            "recurrence_rule as well to make the event recur"
                        )

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

    @staticmethod
    def _is_date_only(value: str) -> bool:
        """Whether an ISO string names a whole day rather than an instant."""
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            return False
        return True

    def _parse_todo_date(self, value: str, *, date_only: bool) -> dt.date | dt.datetime:
        """Parse a VTODO DUE/DTSTART value, keeping a whole day as a DATE.

        RFC 5545 allows both DATE and DATE-TIME here, and ``date_only`` says
        which the pair resolved to. Widening a date to midnight UTC (issue
        #1274) both changes the value the caller gets back and lands on the
        wrong day for anyone west of UTC.
        """
        return (
            dt.date.fromisoformat(value)
            if date_only
            else self._ensure_timezone_aware(value)
        )

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

        # Due / start dates. RFC 5545 §3.8.2.3 ties DUE's value type to
        # DTSTART's, so the pair decides together: date-only on every supplied
        # side is an all-day task (DATE), a time on either side keeps both as
        # DATE-TIME.
        due = todo_data.get("due", "")
        dtstart = todo_data.get("dtstart", "")
        date_only = all(self._is_date_only(v) for v in (due, dtstart) if v)

        if due:
            todo.add("due", vDDDTypes(self._parse_todo_date(due, date_only=date_only)))

        if dtstart:
            todo.add(
                "dtstart",
                vDDDTypes(self._parse_todo_date(dtstart, date_only=date_only)),
            )

        # Completed timestamp
        completed = todo_data.get("completed", "")
        if completed:
            completed_dt = self._ensure_timezone_aware(completed)
            todo.add("completed", vDDDTypes(completed_dt))

        # Categories
        categories = todo_data.get("categories", "")
        if categories:
            todo.add("categories", categories.split(","))

        # Alarms/reminders
        self._apply_reminders(
            todo, todo_data, todo_data.get("summary", ""), _TODO_REMINDER_DESCRIPTION
        )

        # Add timestamps
        now = dt.datetime.now(dt.UTC)
        todo.add("created", now)
        todo.add("dtstamp", now)
        todo.add("last-modified", now)

        cal.add_component(todo)
        return cal.to_ical().decode("utf-8")

    @staticmethod
    def _select_master_vtodo(cal: Any) -> Any | None:
        """Pick the master VTODO from a resource.

        A recurring todo may carry its modified instances in the same resource,
        each tagged with a RECURRENCE-ID. Taking the first component in document
        order would let such an override shadow the series, so prefer the
        component without a RECURRENCE-ID.
        """
        components = list(cal.walk("VTODO"))
        for component in components:
            if "recurrence-id" not in component:
                return component
        return components[0] if components else None

    def _pending_todo_occurrences(
        self, cal: Any, master: Any, now: dt.datetime | None = None
    ) -> dict[str, Any]:
        """Summarise the unfinished occurrences of a recurring VTODO.

        CalDAV never expands VTODO recurrences: a ``calendar-query`` hands back
        the whole resource, and the master component's DTSTART/DUE describe the
        *first* instance of the series. Reporting those verbatim makes a live
        monthly chore from 2023 look years overdue, so the recurrence set is
        expanded client-side — the same approach
        :meth:`_expand_event_occurrences` already takes for VEVENT.

        Expansion applies EXDATE and RECURRENCE-ID overrides, which is what
        makes per-instance completion visible: clients that materialise
        recurrences write a ``STATUS:COMPLETED`` override per finished instance.
        An occurrence counts as pending when it has started
        (``DTSTART <= now``) and is not done — the same rule task apps use to
        decide what to show, so the result matches what the user sees there.

        Returns ``pending_count`` plus the oldest and newest pending occurrence.
        An empty dict means the recurrence could not be resolved at all, leaving
        the master's DTSTART/DUE as the only answer.
        """
        dtstart = master.get("dtstart")
        if not master.get("rrule") or not dtstart:
            return {}

        now = now or dt.datetime.now(dt.UTC)
        # `between` returns occurrences overlapping the window, so an instance
        # that has started but is not yet due is included.
        window_start = max(_as_utc_datetime(dtstart.dt), now - _PENDING_MAX_LOOKBACK)
        if window_start >= now:
            # The series only begins in the future, so nothing has started and
            # there is no backlog. Querying this span would ask for a window
            # that ends before it starts, which the expander rejects.
            return {"pending_count": 0}

        try:
            occurrences = recurring_ical_events.of(cal, components=["VTODO"]).between(
                window_start, now
            )
        except Exception as e:
            logger.warning(
                "Client-side VTODO recurrence expansion failed (%s); "
                "falling back to the master DTSTART/DUE",
                e,
            )
            return {}

        pending = sorted(
            (
                occ
                for occ in occurrences
                if occ.get("dtstart") and not _occurrence_is_done(occ)
            ),
            key=lambda occ: _as_utc_datetime(occ.get("dtstart").dt),
        )
        if not pending:
            # Every started occurrence is done — the series is up to date.
            return {"pending_count": 0}

        oldest, newest = pending[0], pending[-1]
        occurrence_data: dict[str, Any] = {
            "pending_count": len(pending),
            "oldest_pending_dtstart": oldest.get("dtstart").dt.isoformat(),
            "current_dtstart": newest.get("dtstart").dt.isoformat(),
        }
        if oldest.get("due"):
            occurrence_data["oldest_pending_due"] = oldest.get("due").dt.isoformat()
        if newest.get("due"):
            occurrence_data["current_due"] = newest.get("due").dt.isoformat()
        return occurrence_data

    def _parse_ical_todo(
        self, ical_text: str, now: dt.datetime | None = None
    ) -> dict[str, Any] | None:
        """Parse iCalendar text and extract todo data.

        ``now`` overrides the reference instant used to resolve which occurrence
        of a recurring todo is the current one (injected by tests).
        """
        try:
            cal = Calendar.from_ical(ical_text)
            component = self._select_master_vtodo(cal)
            if component is None:
                return None

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

            # Handle recurrence. DTSTART/DUE stay as stored so that updates keep
            # addressing the series, while the pending_* / current_* fields
            # describe the instances a caller should reason about today.
            rrule = component.get("rrule")
            if rrule:
                todo_data["recurring"] = True
                todo_data["recurrence_rule"] = _rrule_to_string(rrule)
                todo_data.update(self._pending_todo_occurrences(cal, component, now))

            reminders = self._extract_valarms(component)
            if reminders:
                todo_data["reminders"] = reminders

            return todo_data

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

                    # Due / start dates, paired the same way as
                    # _create_ical_todo — except a side that isn't being
                    # updated votes with the value type already stored.
                    supplied = {
                        prop: todo_data[prop]
                        for prop in ("due", "dtstart")
                        if todo_data.get(prop)
                    }
                    date_only = all(
                        self._is_date_only(v) for v in supplied.values()
                    ) and all(
                        self._stored_is_all_day(component, prop.upper()) is not False
                        for prop in ("due", "dtstart")
                        if prop not in supplied
                    )

                    # A partial update cannot flip one half of the pair on its
                    # own: writing the supplied side as a DATE-TIME while the
                    # side left out stays a stored DATE is exactly the mismatch
                    # §3.8.2.3 forbids, and there is no defensible time of day
                    # to invent for the property the caller didn't mention.
                    # _validate_all_day_flip rejects the same flip for VEVENT.
                    stranded = [
                        prop
                        for prop in ("due", "dtstart")
                        if supplied
                        and not date_only
                        and prop not in supplied
                        and self._stored_is_all_day(component, prop.upper())
                    ]
                    if stranded:
                        raise ValueError(
                            "changing a whole-day todo to a timed one requires "
                            f"passing {' and '.join(sorted(supplied.keys() | set(stranded)))} "
                            "together, so DUE and DTSTART cannot end up with "
                            "mismatched value types"
                        )

                    for prop, value in supplied.items():
                        parsed = self._parse_todo_date(value, date_only=date_only)
                        component[prop.upper()] = vDDDTypes(parsed)
                        logger.debug("Set %s to %s", prop.upper(), parsed)

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

                    # Handle reminders (VALARM)
                    self._apply_reminders(
                        component,
                        todo_data,
                        todo_data.get("summary") or str(component.get("summary", "")),
                        _TODO_REMINDER_DESCRIPTION,
                    )

                    # Update timestamps
                    now = dt.datetime.now(dt.UTC)
                    component["LAST-MODIFIED"] = vDDDTypes(now)
                    component["DTSTAMP"] = vDDDTypes(now)

                    break

            return cal.to_ical().decode("utf-8")

        except ValueError:
            # A rejected update — a pairing conflict, or a date string that
            # won't parse — is the caller's to fix. The fallback below rebuilds
            # the todo from the *partial* update dict, so swallowing this would
            # answer "invalid input" by silently dropping every stored property
            # the caller didn't happen to pass, and reporting success.
            # ``_merge_ical_properties`` dropped its identical fallback for
            # VEVENT for exactly that reason.
            raise
        except Exception as e:
            logger.error("Error merging iCal todo properties: %s", e)
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
