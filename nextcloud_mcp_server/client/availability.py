"""Interval arithmetic behind ``nc_calendar_find_availability``.

Everything here is pure: it takes already-fetched event dicts (or an
already-fetched VFREEBUSY blob) and returns time spans. The I/O lives in
``CalendarClient.find_availability``, which is what makes this testable
without a CalDAV server.

The shape of the answer is *maximal free windows*, not a grid of candidate
start times: a free 09:00-12:00 is returned once as a 180-minute slot rather
than as five overlapping 60-minute ones. The caller wanted to know where the
gaps are, and a grid multiplies the answer by an arbitrary granularity
constant nobody asked for.
"""

import datetime as dt
import logging
from typing import Any

from icalendar import Calendar

logger = logging.getLogger(__name__)

# A span is a half-open [start, end) interval of aware datetimes.
Span = tuple[dt.datetime, dt.datetime]

# "Business hours" in the sense the tool's parameter means it.
BUSINESS_START = dt.time(9, 0)
BUSINESS_END = dt.time(17, 0)

# RFC 5545 3.2.9: FBTYPE=FREE marks a period that does *not* consume time.
# Everything else (BUSY, BUSY-TENTATIVE, BUSY-UNAVAILABLE, absent) does.
_FREE_FBTYPE = "FREE"


def parse_time_ranges(value: str | list[str] | None) -> list[tuple[dt.time, dt.time]]:
    """Parse ``"09:00-12:00,14:00-17:00"`` into time pairs.

    Accepts the already-split list the MCP layer produces as well as the raw
    comma-separated string. Malformed entries are logged and skipped rather
    than raising: one typo in a preferred-times hint should not fail the whole
    availability query.
    """
    if not value:
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)

    ranges: list[tuple[dt.time, dt.time]] = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        start_text, _, end_text = text.partition("-")
        try:
            start = dt.time.fromisoformat(start_text.strip())
            end = dt.time.fromisoformat(end_text.strip())
        except ValueError:
            logger.warning("Ignoring malformed preferred time range %r", text)
            continue
        if start >= end:
            logger.warning("Ignoring inverted preferred time range %r", text)
            continue
        ranges.append((start, end))
    return ranges


def to_aware(value: Any, tz: dt.tzinfo) -> dt.datetime | None:
    """Coerce an event's ISO datetime (or date) string into an aware datetime.

    Naive values are floating local time in RFC 5545 terms, so they are read in
    ``tz`` -- the same zone the daily windows are built in, which is what makes
    "does this event overlap business hours" answerable at all.
    """
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time(0, 0), tzinfo=tz)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Unparseable datetime %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def event_is_busy(event: dict[str, Any], *, include_all_day: bool) -> bool:
    """Whether an event should block time.

    Four things make an event non-blocking, and all four matter on real
    calendars:

    * its calendar is marked "never show me as busy"
      (``schedule-calendar-transp: transparent``, RFC 4791 5.2.9);
    * the event itself is ``TRANSP:TRANSPARENT`` (RFC 5545 3.8.2.7);
    * it is cancelled;
    * it is all-day and the caller did not opt in. Birthdays, name days and
      subscribed school-holiday feeds are all-day and OPAQUE, so honouring them
      erases entire working days -- the exact complaint in issue #1394.
    """
    if event.get("calendar_transparent"):
        return False
    if str(event.get("transp") or "OPAQUE").upper() == "TRANSPARENT":
        return False
    if str(event.get("status") or "").upper() == "CANCELLED":
        return False
    if event.get("all_day") and not include_all_day:
        return False
    return True


def busy_spans_from_events(
    events: list[dict[str, Any]],
    *,
    tz: dt.tzinfo,
    include_all_day: bool = False,
) -> list[Span]:
    """Busy spans for the events that actually consume time."""
    spans: list[Span] = []
    for event in events:
        if not event_is_busy(event, include_all_day=include_all_day):
            continue

        start = to_aware(event.get("start_datetime"), tz)
        if start is None:
            continue

        end = to_aware(event.get("end_datetime"), tz)
        if end is None and event.get("all_day"):
            # An all-day event with no DTEND covers its one day.
            end = start + dt.timedelta(days=1)
        if end is None or end <= start:
            # Zero-length (and inverted) events mark an instant, not a block.
            continue
        spans.append((start, end))
    return spans


def busy_spans_from_vfreebusy(data: str, tz: dt.tzinfo) -> list[Span]:
    """Busy spans from an RFC 6638 free/busy reply for one attendee.

    Periods come back as ``start/end`` or ``start/duration``; icalendar hands
    both back as a ``vPeriod`` whose ``.dt`` is a 2-tuple, with the FBTYPE
    parameter attached per period rather than per FREEBUSY line.
    """
    try:
        calendar = Calendar.from_ical(data)
    except Exception as e:  # icalendar raises bare ValueError subclasses
        logger.warning("Unparseable free/busy reply: %s", e)
        return []

    spans: list[Span] = []
    for component in calendar.walk("VFREEBUSY"):
        periods = component.get("freebusy")
        if periods is None:
            continue
        for period in periods if isinstance(periods, list) else [periods]:
            fbtype = str(period.params.get("FBTYPE", "BUSY")).upper()
            if fbtype == _FREE_FBTYPE:
                continue
            start, end_or_duration = period.dt
            end = (
                start + end_or_duration
                if isinstance(end_or_duration, dt.timedelta)
                else end_or_duration
            )
            start = to_aware(start, tz)
            end = to_aware(end, tz)
            if start is None or end is None or end <= start:
                continue
            spans.append((start, end))
    return spans


def merge_spans(spans: list[Span]) -> list[Span]:
    """Sort and coalesce overlapping/touching spans."""
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def daily_windows(
    start: dt.datetime,
    end: dt.datetime,
    *,
    tz: dt.tzinfo,
    business_hours_only: bool = True,
    exclude_weekends: bool = True,
    preferred_times: list[tuple[dt.time, dt.time]] | None = None,
) -> list[Span]:
    """The spans the caller is willing to meet in, day by day, clipped to
    ``[start, end)``.

    ``preferred_times`` *replaces* business hours rather than intersecting with
    them. Intersecting would make ``preferred_times="08:00-09:00"`` return
    nothing at all under the default ``business_hours_only=True`` -- an empty
    answer that looks like "you are fully booked", which is the failure mode
    this whole tool is being fixed for.
    """
    windows: list[Span] = []
    day = start.astimezone(tz).date()
    last_day = end.astimezone(tz).date()
    ranges = preferred_times or (
        [(BUSINESS_START, BUSINESS_END)] if business_hours_only else []
    )

    while day <= last_day:
        if exclude_weekends and day.weekday() >= 5:
            day += dt.timedelta(days=1)
            continue

        if ranges:
            day_spans = [
                (
                    dt.datetime.combine(day, from_time, tzinfo=tz),
                    dt.datetime.combine(day, to_time, tzinfo=tz),
                )
                for from_time, to_time in ranges
            ]
        else:
            midnight = dt.datetime.combine(day, dt.time(0, 0), tzinfo=tz)
            day_spans = [(midnight, midnight + dt.timedelta(days=1))]

        for window_start, window_end in day_spans:
            clipped_start = max(window_start, start)
            clipped_end = min(window_end, end)
            if clipped_start < clipped_end:
                windows.append((clipped_start, clipped_end))

        day += dt.timedelta(days=1)

    return sorted(windows)


def free_slots(
    windows: list[Span], busy: list[Span], minimum: dt.timedelta
) -> list[Span]:
    """Subtract busy spans from the candidate windows, keeping what is long enough."""
    busy = merge_spans(busy)
    slots: list[Span] = []

    for window_start, window_end in windows:
        cursor = window_start
        for busy_start, busy_end in busy:
            if busy_end <= cursor:
                continue
            if busy_start >= window_end:
                break
            if busy_start - cursor >= minimum:
                slots.append((cursor, busy_start))
            cursor = max(cursor, busy_end)
            if cursor >= window_end:
                break
        if window_end - cursor >= minimum:
            slots.append((cursor, window_end))

    return slots


def slot_to_dict(span: Span) -> dict[str, Any]:
    """Render a span the way ``AvailabilitySlot`` expects it."""
    start, end = span
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_minutes": int((end - start).total_seconds() // 60),
        "date": start.date().isoformat(),
    }
