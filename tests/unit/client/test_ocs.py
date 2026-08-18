"""Unit tests for the shared OCS envelope handling.

Two things are worth pinning here: that the parser survives every malformed
shape rather than raising a ``KeyError`` the caller cannot act on, and that a
997 is described by its two real causes instead of being reported as a generic
server failure.
"""

import pytest

from nextcloud_mcp_server.client.ocs import (
    OCS_REQUEST_HEADERS,
    OCS_STATUS_UNAUTHENTICATED,
    describe_ocs_failure,
    parse_ocs_envelope,
)

pytestmark = pytest.mark.unit


def _envelope(status_code: int, message: str = "OK", data=None) -> dict:
    return {
        "ocs": {
            "meta": {"status": "ok", "statuscode": status_code, "message": message},
            "data": data,
        }
    }


@pytest.mark.parametrize("status_code", [100, 200])
def test_both_documented_success_codes_are_accepted(status_code):
    """100 is OCS v1's success, 200 is v2's -- which one arrives depends only
    on the route, so treating either as failure breaks half the API."""
    envelope = parse_ocs_envelope(_envelope(status_code, data={"id": 1}))

    assert envelope.is_success
    assert envelope.data == {"id": 1}


@pytest.mark.parametrize("status_code", [400, 403, 404, 997, 998])
def test_non_success_codes_are_reported_as_failures(status_code):
    assert not parse_ocs_envelope(_envelope(status_code)).is_success


def test_997_names_both_of_its_causes():
    """The whole reason 997 gets special wording.

    Reported generically it sends the reader looking for a server fault that is
    not there -- the cause is either rejected credentials or a missing
    ``OCS-APIRequest`` header, and the message has to say both.
    """
    described = describe_ocs_failure(
        OCS_STATUS_UNAUTHENTICATED, "Current user is not logged in"
    )

    assert "unauthenticated" in described
    assert "OCS-APIRequest" in described
    assert str(OCS_STATUS_UNAUTHENTICATED) in described


def test_other_failures_keep_the_server_message():
    """Only 997 is reworded; everything else reports what the server said."""
    described = describe_ocs_failure(404, "Wrong path, file/folder doesn't exist")

    assert "Wrong path, file/folder doesn't exist" in described
    assert "OCS-APIRequest" not in described


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not json",
        [],
        {},
        {"ocs": None},
        {"ocs": "nope"},
        {"ocs": {"meta": {"statuscode": "not-a-number"}}},
    ],
)
def test_unparseable_payloads_report_failure_without_raising(payload):
    """A body that is not a usable envelope must describe itself, not raise.

    Every caller is already handling a failed request when it gets here, so a
    ``KeyError``/``TypeError`` from parsing would replace a diagnosable server
    error with an opaque one.
    """
    envelope = parse_ocs_envelope(payload)

    assert not envelope.is_success
    assert envelope.message


@pytest.mark.parametrize("payload", [{"ocs": {}}, {"ocs": {"meta": None}}])
def test_absent_meta_is_treated_as_success(payload):
    """An envelope with no usable ``meta`` defaults to 200, i.e. success.

    Deliberate: all three clients did this before the extraction
    (``meta.get("statuscode", 200)``), and OCS omits ``meta`` only on responses
    that succeeded. Tightening it to a failure would be a behaviour change
    smuggled in under a refactor, so it is pinned here rather than left to be
    "corrected" by someone reading the parser in isolation.
    """
    assert parse_ocs_envelope(payload).is_success


def test_missing_data_key_is_distinguishable_from_null_data():
    """``has_data`` separates "no data field" from "data was null".

    The collectives client treats the former as a malformed response; the
    distinction is lost if callers only test ``data is None``.
    """
    absent = parse_ocs_envelope({"ocs": {"meta": {"statuscode": 200}}})
    present = parse_ocs_envelope(_envelope(200, data=None))

    assert not absent.has_data
    assert present.has_data


def test_request_headers_carry_the_csrf_header():
    """Shipping the header from one place is what stops a new call site
    forgetting it and then hitting the 997 it just made confusing."""
    assert OCS_REQUEST_HEADERS["OCS-APIRequest"] == "true"
