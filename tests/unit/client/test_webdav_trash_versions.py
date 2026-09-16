"""Parsing of the trash-bin and version collections.

Both live on dedicated DAV endpoints and answer with a PROPFIND multistatus.
The collection itself shows up as a response element and has to be skipped,
otherwise it would be reported as a deleted file or a stored version.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from nextcloud_mcp_server.client.webdav import WebDAVClient

pytestmark = pytest.mark.unit


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


class TestOcsHeaderPresent:
    """Every DAV call the client makes marks itself as API traffic, or
    Nextcloud's CSRF guard may reject it as browser traffic instead."""

    async def test_list_trash(self, mocker):
        client, http = _client_with_response(mocker)
        await client.list_trash()
        assert http.request.call_args.kwargs["headers"]["OCS-APIRequest"] == "true"

    async def test_restore_from_trash(self, mocker):
        client, http = _client_with_response(mocker)
        await client.restore_from_trash("42.d123")
        assert http.request.call_args.kwargs["headers"]["OCS-APIRequest"] == "true"

    async def test_list_versions(self, mocker):
        client, http = _client_with_response(mocker)
        await client.list_versions("doc.txt", file_id=42)
        assert http.request.call_args.kwargs["headers"]["OCS-APIRequest"] == "true"

    async def test_restore_version(self, mocker):
        client, http = _client_with_response(mocker)
        await client.restore_version("doc.txt", "17", file_id=42)
        assert http.request.call_args.kwargs["headers"]["OCS-APIRequest"] == "true"


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
