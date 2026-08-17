"""Unit tests for DAV error surfacing (``client/dav_errors.py``).

The point of the module under test is that a failed DAV request should carry
the explanation the server already sent, and that the three statuses callers
branch on become distinguishable types -- without breaking any handler that
catches plain ``HTTPStatusError``.
"""

import httpx
import pytest

from nextcloud_mcp_server.client.dav_errors import (
    MAX_ERROR_BODY_BYTES,
    DavError,
    DavInsufficientStorage,
    DavLocked,
    DavPreconditionFailed,
    enrich_dav_error,
    parse_dav_error_body,
)

pytestmark = pytest.mark.unit


def _dav_body(exception: str, message: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<d:error xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns">
  <s:exception>{exception}</s:exception>
  <s:message>{message}</s:message>
</d:error>""".encode()


def _error(status: int, content: bytes = b"") -> httpx.HTTPStatusError:
    """Build the HTTPStatusError raise_for_status() would produce."""
    request = httpx.Request(
        "PUT", "https://nc.example.com/remote.php/dav/files/u/a.txt"
    )
    response = httpx.Response(status, content=content, request=request)
    return httpx.HTTPStatusError(f"{status} error", request=request, response=response)


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        (412, DavPreconditionFailed),
        (423, DavLocked),
        (507, DavInsufficientStorage),
    ],
)
def test_branchable_statuses_get_their_own_type(status, expected_type):
    """412/423/507 are the statuses callers branch on, so each gets a type."""
    enriched = enrich_dav_error(_error(status))

    assert isinstance(enriched, expected_type)
    # Still catchable by every handler that predates this module.
    assert isinstance(enriched, httpx.HTTPStatusError)
    assert enriched.response.status_code == status


def test_server_explanation_is_attached_to_the_message():
    """The s:exception/s:message pair reaches both the message and the attrs."""
    body = _dav_body("Sabre\\DAV\\Exception\\Locked", "File is currently write locked")

    enriched = enrich_dav_error(_error(423, body))

    assert enriched.dav_exception == "Sabre\\DAV\\Exception\\Locked"
    assert enriched.dav_message == "File is currently write locked"
    assert "File is currently write locked" in str(enriched)


def test_dav_detail_is_surfaced_for_unmapped_statuses_too():
    """A 500 has no dedicated type but still carries its explanation.

    This is the empty-<d:where> case: the server said ``TypeError`` in a body
    that used to be discarded, leaving only "HTTP 500" to debug from.
    """
    body = _dav_body("TypeError", "A type error occurred.")

    enriched = enrich_dav_error(_error(500, body))

    assert type(enriched) is DavError
    assert enriched.dav_exception == "TypeError"
    assert "A type error occurred." in str(enriched)


def test_non_dav_error_is_returned_untouched():
    """An OCS/JSON failure is not a DAV error and must pass through unchanged."""
    original = _error(400, b'{"ocs":{"meta":{"statuscode":400}}}')

    assert enrich_dav_error(original) is original


def test_malformed_xml_does_not_raise():
    """A truncated or non-XML body must not turn a failure into a crash."""
    original = _error(403, b"<d:error><s:exception>unclosed")

    assert enrich_dav_error(original) is original


def test_oversized_body_is_not_parsed():
    """Bodies too large to be a DAV error document are skipped, not parsed."""
    oversized = b"<d:error>" + b"x" * (MAX_ERROR_BODY_BYTES + 1)

    assert parse_dav_error_body(_error(500, oversized).response) == (None, None)


def test_non_bytes_body_yields_no_detail(mocker):
    """A response whose body is not bytes must degrade, never raise.

    Regression: the first cut called ``len(body)`` unguarded, so a mocked
    response (``.content`` returning a ``Mock``) raised ``TypeError`` *from
    inside the error handler*, replacing the caller's real failure with a
    nonsense one. Anything raised here masks the error being reported.
    """
    response = mocker.Mock()
    response.content = mocker.Mock()

    assert parse_dav_error_body(response) == (None, None)


def test_unread_streaming_response_yields_no_detail():
    """A streaming response raises before the body is read -- degrade quietly.

    ``_stream_request`` calls ``raise_for_status()`` inside the stream context,
    so the body is not available. Reading it there is not worth a second
    network-facing read on an already-failing download.
    """
    request = httpx.Request(
        "GET", "https://nc.example.com/remote.php/dav/files/u/a.bin"
    )
    response = httpx.Response(
        507, request=request, stream=httpx.ByteStream(_dav_body("Quota", "full"))
    )

    # Precondition: this is genuinely an unread stream.
    with pytest.raises(httpx.ResponseNotRead):
        _ = response.content

    assert parse_dav_error_body(response) == (None, None)
