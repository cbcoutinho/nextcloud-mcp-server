"""Parsing of the trash-bin and version collections.

Both live on dedicated DAV endpoints and answer with a PROPFIND multistatus.
The collection itself shows up as a response element and has to be skipped,
otherwise it would be reported as a deleted file or a stored version.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock
from xml.sax.saxutils import escape as xml_escape

import pytest
from httpx2 import AsyncClient
from hypothesis import event, example, given
from hypothesis import strategies as st
from nextcloud_client.webdav import (
    VersionLabelRejected,
    WebDAVClient,
    _http_date,
    xml_text_problem,
)

pytestmark = pytest.mark.unit


@given(st.text())
@example("\x00")
@example("\x0b")
@example(chr(0xFFFE))  # a noncharacter; spelled out so formatters keep it visible
@example("a < b & </x> ]]>")
@example("\U0001f600 emoji")
def test_xml_text_problem_matches_what_xml_can_carry(text):
    """Accepted text survives escaping and parsing intact; rejected text
    would not parse at all. (XML normalizes a bare CR to LF on parse.)"""
    document = f"<x>{xml_escape(text)}</x>"
    try:
        parsed = ET.fromstring(document).text or ""
    except ET.ParseError:
        assert xml_text_problem(text) is not None
        return
    assert xml_text_problem(text) is None
    assert parsed == text.replace("\r\n", "\n").replace("\r", "\n")


def _client() -> WebDAVClient:
    """The parsers only read the response; skip __init__."""
    return WebDAVClient.__new__(WebDAVClient)


class TestPropfindBody:
    def test_requests_every_property(self):
        body = _client()._propfind_body(WebDAVClient._TRASH_PROPS)
        assert "trashbin-filename" in body
        assert "trashbin-original-location" in body
        assert "getcontentlength" in body

    def test_uses_the_right_namespace_prefix(self):
        body = _client()._propfind_body(WebDAVClient._VERSION_PROPS)
        # DAV: properties use d:, Nextcloud's own use nc:
        assert "<d:getcontentlength />" in body
        assert "<nc:version-label />" in body

    def test_is_well_formed(self):
        from lxml import etree

        body = _client()._propfind_body(WebDAVClient._TRASH_PROPS)
        etree.fromstring(body.encode("utf-8"))  # raises if malformed


# --- OCS-APIRequest header and the file_id shortcut -------------------------
#
# Both bugs surfaced in review: the four new DAV calls omitted the
# OCS-APIRequest header every other call in this module sets (Nextcloud may
# treat an unmarked PROPFIND/MOVE as CSRF-protected browser traffic and
# reject it), and list_versions/restore_version each did their own
# get_fileid lookup even when the MCP tool layer had already resolved one
# for the excluded-tag guard, doubling the DAV round-trips per call.

_EMPTY_MULTISTATUS = (
    b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>'
)


def _client_with_response(mocker, content: bytes = _EMPTY_MULTISTATUS):
    """A WebDAVClient whose principal is already known and whose transport
    returns a canned response for whatever it is asked."""
    http = mocker.AsyncMock(spec=AsyncClient)
    response = mocker.Mock()
    response.content = content
    response.status_code = 200
    response.raise_for_status = mocker.Mock()
    http.request = AsyncMock(return_value=response)
    client = WebDAVClient(http, "testuser")
    client._principal_discovered = True
    return client, http


class TestFileIdShortcut:
    """A caller that already resolved the file id (the MCP tool layer, for
    its excluded-tag guard) should not pay for a second get_fileid round-trip."""

    async def test_list_versions_skips_lookup_when_file_id_given(self, mocker):
        client, _ = _client_with_response(mocker)
        client.get_fileid = AsyncMock(
            side_effect=AssertionError("should not be called")
        )
        result = await client.list_versions("doc.txt", file_id=42)
        assert result["file_id"] == 42
        client.get_fileid.assert_not_called()

    async def test_list_versions_resolves_when_file_id_omitted(self, mocker):
        client, _ = _client_with_response(mocker)
        client.get_fileid = AsyncMock(return_value="42")
        await client.list_versions("doc.txt")
        client.get_fileid.assert_awaited_once_with("doc.txt")

    async def test_restore_version_skips_lookup_when_file_id_given(self, mocker):
        client, _ = _client_with_response(mocker)
        client.get_fileid = AsyncMock(
            side_effect=AssertionError("should not be called")
        )
        await client.restore_version("doc.txt", "17", file_id=42)
        client.get_fileid.assert_not_called()

    async def test_restore_version_resolves_when_file_id_omitted(self, mocker):
        client, _ = _client_with_response(mocker)
        client.get_fileid = AsyncMock(return_value="42")
        await client.restore_version("doc.txt", "17")
        client.get_fileid.assert_awaited_once_with("doc.txt")


# --- Version metadata, ordering and the current revision (GH #1931) ---------

_VERSIONS_MULTISTATUS = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/versions/testuser/versions/42/</d:href>
    <d:propstat><d:prop/><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/versions/testuser/versions/42/1700000000</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontentlength>5</d:getcontentlength>
        <d:getlastmodified>Tue, 14 Nov 2023 22:13:20 GMT</d:getlastmodified>
        <d:getcontenttype>text/markdown</d:getcontenttype>
        <d:getetag>"1700000000"</d:getetag>
        <nc:version-label>checkpoint</nc:version-label>
        <nc:version-author>alice</nc:version-author>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/versions/testuser/versions/42/1700000100</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontentlength>6</d:getcontentlength>
        <d:getlastmodified>Tue, 14 Nov 2023 22:15:00 GMT</d:getlastmodified>
        <d:getcontenttype>text/markdown</d:getcontenttype>
        <d:getetag>"1700000100"</d:getetag>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
    <d:propstat>
      <d:prop><nc:version-label/><nc:version-author/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""


class TestListVersionsMetadata:
    async def test_newest_first_whatever_the_server_order(self, mocker):
        # Nextcloud answers in database order, here oldest first.
        client, _ = _client_with_response(mocker, _VERSIONS_MULTISTATUS)
        result = await client.list_versions("doc.md", file_id=42)
        assert [v["version_id"] for v in result["versions"]] == [
            "1700000100",
            "1700000000",
        ]

    async def test_reads_author_type_and_revision_etag(self, mocker):
        client, _ = _client_with_response(mocker, _VERSIONS_MULTISTATUS)
        result = await client.list_versions("doc.md", file_id=42)
        old = result["versions"][1]
        assert old["author"] == "alice"
        assert old["label"] == "checkpoint"
        assert old["content_type"] == "text/markdown"
        assert old["revision_etag"] == "1700000000"  # quotes stripped
        # The 404 propstat block is not folded in as an empty value.
        assert result["versions"][0]["author"] is None

    async def test_marks_the_current_revision(self, mocker):
        client, _ = _client_with_response(mocker, _VERSIONS_MULTISTATUS)
        result = await client.list_versions(
            "doc.md", file_id=42, current_modified="Tue, 14 Nov 2023 22:15:00 GMT"
        )
        assert [v["is_current"] for v in result["versions"]] == [True, False]

    async def test_current_is_unknown_without_the_live_mtime(self, mocker):
        client, _ = _client_with_response(mocker, _VERSIONS_MULTISTATUS)
        result = await client.list_versions("doc.md", file_id=42)
        assert {v["is_current"] for v in result["versions"]} == {None}


def _field(valid_lo: int, valid_hi: int, wide_hi: int) -> st.SearchStrategy[int]:
    """Usually in range, sometimes not, so both outcomes are common."""
    return st.integers(valid_lo, valid_hi) | st.integers(-1, wide_hi)


#: RFC 1123-shaped strings whose fields may each be out of range.
_date_shaped = st.builds(
    "{}, {} {} {} {}:{}:{} {}".format,
    st.sampled_from(["Tue", "Xyz", ""]),
    _field(1, 28, 99),
    st.sampled_from(["Nov", "Feb", "Foo"]),
    _field(1900, 9999, 100_000),
    _field(0, 23, 99),
    _field(0, 59, 99),
    _field(0, 59, 99),
    st.sampled_from(["GMT", "-0000", "UT", "EST", "XYZ"])
    | st.from_regex(r"[+-][0-9]{4}", fullmatch=True),
)


class TestHttpDate:
    @given(st.none() | st.text(st.characters(codec="utf-8")) | _date_shaped)
    @example(None)
    @example("")
    @example("not a date")
    @example("Tue, 14 Nov 99999 22:15:00 GMT")  # year out of range
    @example("Tue, 14 Nov 2023 22:15:00 -0000")  # parses naive
    def test_any_value_parses_to_an_aware_datetime_or_none(self, value):
        """A server-supplied string: never raises, never yields a naive datetime
        (sorting must not mix naive and aware)."""
        parsed = _http_date(value)
        event("parsed" if parsed is not None else "unusable")
        assert parsed is None or parsed.tzinfo is not None

    @given(
        st.datetimes(datetime(1900, 1, 2), datetime(9998, 12, 30)),
        st.integers(-23 * 60 - 59, 23 * 60 + 59),
    )
    def test_an_rfc_1123_date_round_trips(self, utc, offset_minutes):
        offset = timedelta(minutes=offset_minutes)
        instant = utc.replace(microsecond=0, tzinfo=timezone.utc)
        assert (
            _http_date(format_datetime(instant.astimezone(timezone(offset)))) == instant
        )

    def test_naive_zone_is_treated_as_utc(self):
        assert _http_date("Tue, 14 Nov 2023 22:15:00 -0000") == _http_date(
            "Tue, 14 Nov 2023 22:15:00 GMT"
        )


class TestStreamVersion:
    async def test_gets_the_version_resource(self, mocker, tmp_path):
        client, _ = _client_with_response(mocker)
        stream = mocker.patch.object(
            client,
            "_stream_dav_to_file",
            AsyncMock(return_value=(5, "text/plain", "1")),
        )
        await client.stream_version_to_file(42, "1700000000", tmp_path / "out")
        assert (
            stream.await_args.args[0]
            == "/remote.php/dav/versions/testuser/versions/42/1700000000"
        )


# --- Version labels (GH #1931) -----------------------------------------------

_PROPPATCH_OK = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/versions/testuser/versions/42/17</d:href>
    <d:propstat>
      <d:prop><nc:version-label/></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""

_PROPPATCH_REFUSED = _PROPPATCH_OK.replace(b"200 OK", b"403 Forbidden")


class TestSetVersionLabel:
    async def _sent_body(self, mocker, label: str) -> str:
        client, http = _client_with_response(mocker, _PROPPATCH_OK)
        await client.set_version_label("doc.txt", "17", label, file_id=42)
        call = http.request.await_args
        assert call.args[0] == "PROPPATCH"
        assert call.args[1].endswith("/remote.php/dav/versions/testuser/versions/42/17")
        return call.kwargs["content"]

    async def test_empty_success_body_is_accepted(self, mocker):
        """A 204-style minimal success carries no multistatus to inspect."""
        client, _ = _client_with_response(mocker, b"")
        result = await client.set_version_label("doc.txt", "17", "x", file_id=42)
        assert result["label"] == "x"

    async def test_non_xml_body_is_a_clear_rejection(self, mocker):
        client, _ = _client_with_response(mocker, b"<html>proxy page")
        with pytest.raises(VersionLabelRejected, match="not a DAV multistatus"):
            await client.set_version_label("doc.txt", "17", "x", file_id=42)

    async def test_unrepresentable_label_is_refused_before_any_request(self, mocker):
        client, http = _client_with_response(mocker, _PROPPATCH_OK)
        with pytest.raises(ValueError, match="U\\+0000"):
            await client.set_version_label("doc.txt", "17", "a\x00b", file_id=42)
        http.request.assert_not_called()

    async def test_sets_the_label(self, mocker):
        body = await self._sent_body(mocker, "checkpoint")
        assert "<nc:version-label>checkpoint</nc:version-label>" in body

    async def test_clearing_sends_an_explicit_empty_element(self, mocker):
        # Self-closing parses as null server-side, which Nextcloud's
        # string-typed handler rejects with a TypeError.
        body = await self._sent_body(mocker, "")
        assert "<nc:version-label></nc:version-label>" in body
        assert "<nc:version-label/>" not in body
        assert "<nc:version-label />" not in body

    async def test_label_is_xml_escaped(self, mocker):
        from lxml import etree

        body = await self._sent_body(mocker, "a < b & </nc:version-label>")
        tree = etree.fromstring(body.encode("utf-8"))
        label = tree.find(".//{http://nextcloud.org/ns}version-label")
        assert label.text == "a < b & </nc:version-label>"

    async def test_refused_property_raises(self, mocker):
        client, _ = _client_with_response(mocker, _PROPPATCH_REFUSED)
        with pytest.raises(VersionLabelRejected, match=r": 403 Forbidden$"):
            await client.set_version_label("doc.txt", "17", "x", file_id=42)

    @pytest.mark.parametrize(
        "status",
        [b"", b"garbage", None],
        ids=["empty-status", "unparseable-status", "no-status-element"],
    )
    async def test_unconfirmed_success_is_not_success(self, mocker, status):
        """A multistatus with no readable propstat status never reads as a
        successful write."""
        body = _PROPPATCH_OK.replace(b"<d:status>HTTP/1.1 200 OK</d:status>", b"")
        if status is not None:
            body = body.replace(
                b"</d:prop>", b"</d:prop><d:status>" + status + b"</d:status>"
            )
        client, _ = _client_with_response(mocker, body)
        with pytest.raises(VersionLabelRejected, match="unconfirmed"):
            await client.set_version_label("doc.txt", "17", "x", file_id=42)

    async def test_resolves_file_id_when_omitted(self, mocker):
        client, _ = _client_with_response(mocker, _PROPPATCH_OK)
        client.get_fileid = AsyncMock(return_value="42")
        await client.set_version_label("doc.txt", "17", "x")
        client.get_fileid.assert_awaited_once_with("doc.txt")
