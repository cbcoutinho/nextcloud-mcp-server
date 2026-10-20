"""Shared handling for Nextcloud's OCS response envelope.

Every OCS endpoint wraps its payload the same way::

    {"ocs": {"meta": {"status": "ok", "statuscode": 200, "message": "OK"},
             "data": {...}}}

Two properties of that envelope bite callers who only check the HTTP status:

* **The v1 trap.** ``/ocs/v1.php`` answers *every* request with HTTP 200 and
  puts the real outcome in ``meta.statuscode``, where success is ``100``.
  ``/ocs/v2.php`` mirrors the OCS code onto the HTTP status and uses ``200``.
  Both codes therefore mean success, depending only on which route was called.

* **997 is not a server error.** Nextcloud returns it when the request was
  unauthenticated *or* when it omitted the mandatory ``OCS-APIRequest: true``
  header that its CSRF check requires on every OCS call. Reported as a generic
  failure it sends the reader hunting for a server fault that is not there, so
  it gets named explicitly here.

This module deliberately does **not** raise. Three clients parse this envelope
and each raises a different type that its callers already catch --
``OCSError`` (collectives, caught in a dozen places in ``server/collectives``),
``HTTPStatusError`` (mail), and ``RuntimeError`` (sharing). Centralising the
*parsing* and the *wording* is safe; centralising the raising would change
three caller contracts at once.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, NamedTuple

#: Extra headers a JSON-returning ``/ocs/v2.php`` call sends: OCS answers in XML
#: unless asked for JSON.
#:
#: ``OCS-APIRequest: true`` is deliberately *not* here. Every Nextcloud-bound
#: httpx client sends it as a client default (``http.NEXTCLOUD_DEFAULT_HEADERS``
#: via ``nextcloud_httpx_client``), so no call site can forget it -- a
#: per-client opt-in is how the Tables v1 routes went without it (GH #1664).
#: ``tests/unit/test_ocs_headers_are_shared`` fails if any module spells it.
#:
#: Read-only on purpose: several clients share this one object, so an in-place
#: edit would be a cross-client bug; ``MappingProxyType`` makes that a
#: ``TypeError`` at the point of the mistake. Consumers take a ``dict(...)`` or
#: ``{**...}`` copy.
#:
#: Header sets that do not want JSON keep their own ``Accept`` (or none):
#: WebDAV (Sabre answers in XML), ``MailClient.get_attachment`` (binary bytes),
#: Deck's REST API (sends ``Content-Type``), and ``api/passwords.py`` (asks for
#: JSON with ``format=json``, which OCS honours in place of the header).
OCS_REQUEST_HEADERS: Mapping[str, str] = MappingProxyType(
    {
        "Accept": "application/json",
    }
)

#: Success codes: ``100`` from OCS v1, ``200`` from v2.
OCS_SUCCESS_STATUS_CODES = frozenset({100, 200})

#: "Unauthorised" in OCS's vocabulary -- bad credentials *or* a missing
#: ``OCS-APIRequest`` header.
OCS_STATUS_UNAUTHENTICATED = 997

_UNAUTHENTICATED_HINT = (
    "unauthenticated — either the credentials were rejected, or the request "
    "omitted the 'OCS-APIRequest: true' header that Nextcloud's CSRF check "
    "requires on OCS routes"
)


class OCSEnvelope(NamedTuple):
    """The parts of an OCS envelope callers act on."""

    status_code: int
    message: str
    data: Any
    has_data: bool

    @property
    def is_success(self) -> bool:
        """True when the OCS code is a documented success (100 or 200).

        This is stricter than the ``< 400`` test the collectives and mail
        clients apply. They keep their own comparison for now rather than being
        silently retightened by a refactor -- converging on one rule needs
        evidence about which sub-400 codes real endpoints return.
        """
        return self.status_code in OCS_SUCCESS_STATUS_CODES


def parse_ocs_envelope(payload: Any) -> OCSEnvelope:
    """Pull ``(statuscode, message, data)`` out of an OCS response body.

    Tolerates every malformed shape seen in practice -- a non-dict body, a
    missing or non-dict ``ocs`` / ``meta``, a non-numeric statuscode -- by
    reporting ``500`` with a description, rather than raising a ``KeyError`` or
    ``TypeError`` that tells the caller nothing about what the server said.
    """
    if not isinstance(payload, dict):
        return OCSEnvelope(
            500, f"Response is not a JSON object: {type(payload).__name__}", None, False
        )

    ocs = payload.get("ocs")
    if not isinstance(ocs, dict):
        return OCSEnvelope(500, "Response is not an OCS envelope", None, False)

    meta = ocs.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    # ``or 200`` covers falsy-but-present values (``0``, ``""``, ``None``) the
    # same way the mail client did before this module existed. Without it an
    # empty statuscode parses to 500, which crosses mail's ``>= 400`` gate and
    # turns a response that used to succeed into a raised error. A genuinely
    # non-numeric value still falls through to 500 -- an unreadable status is
    # not something to report as success.
    raw_status = meta.get("statuscode") or 200
    try:
        status_code = int(raw_status)
    except (TypeError, ValueError):
        status_code = 500

    # One fallback string for all three clients. They previously differed --
    # sharing "Unknown error", collectives "OCS error", mail "Unknown OCS
    # error" -- and consolidating is deliberate rather than incidental: this is
    # the text shown only when the server sent no message at all, so a
    # per-client variant conveys nothing a caller can act on. Called out
    # explicitly because the statuscode default next to it was preserved
    # exactly, and the difference in treatment should not look accidental.
    message = meta.get("message") or "Unknown error"
    return OCSEnvelope(status_code, str(message), ocs.get("data"), "data" in ocs)


def describe_ocs_failure(status_code: int, message: str) -> str:
    """Render an OCS failure, naming 997's two causes rather than guessing."""
    if status_code == OCS_STATUS_UNAUTHENTICATED:
        return f"OCS API error (code {status_code}): {_UNAUTHENTICATED_HINT}"
    return f"OCS API error (code {status_code}): {message}"
