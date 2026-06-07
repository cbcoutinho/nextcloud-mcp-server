"""Unit tests for NextcloudClient orchestration logic.

Currently covers ``find_files_by_tag``: the wrapper that combines
``WebDAVClient.get_tag_by_name``, ``WebDAVClient.get_files_by_tag``, and
``WebDAVClient.find_all_by_type`` to resolve a system tag (and any tagged
folders) into a flat list of files.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nextcloud_mcp_server.client import NextcloudClient, _normalise_search_result


def _make_client() -> Any:
    """Build a NextcloudClient with mocked sub-clients.

    The client constructor opens an httpx session; we don't need it, just
    a stub instance whose ``webdav`` attribute we can replace. Returned as
    ``Any`` so tests can freely reassign mocked methods on the sub-clients
    without fighting the real ``WebDAVClient`` signatures.
    """
    client: Any = NextcloudClient.__new__(NextcloudClient)
    client.username = "alice"
    client.webdav = AsyncMock()
    return client


pytestmark = pytest.mark.unit


def _navigation_response(entries: list[dict]) -> MagicMock:
    """Build a mocked OCS v2 ``core/navigation/apps`` response."""
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json.return_value = {"ocs": {"meta": {}, "data": entries}}
    return response


class TestGetEnabledApps:
    async def test_returns_app_ids_from_navigation(self):
        client = _make_client()
        client._client = AsyncMock()
        client._client.get = AsyncMock(
            return_value=_navigation_response(
                [
                    {"id": "files", "app": "files"},
                    {"id": "notes", "app": "notes"},
                    {"id": "deck", "app": "deck"},
                    {"id": "news", "app": "news"},
                ]
            )
        )

        apps = await client.get_enabled_apps()

        assert apps == {"files", "notes", "deck", "news"}
        # Hits the per-user navigation endpoint, not capabilities.
        assert (
            client._client.get.await_args.args[0] == "/ocs/v2.php/core/navigation/apps"
        )

    async def test_unions_id_and_app_keys(self):
        """When ``id`` and ``app`` differ, both are collected so an enabled
        app is never hidden by an unexpected nav-entry id."""
        client = _make_client()
        client._client = AsyncMock()
        client._client.get = AsyncMock(
            return_value=_navigation_response([{"id": "files_sharing", "app": "files"}])
        )

        apps = await client.get_enabled_apps()

        assert apps == {"files", "files_sharing"}

    async def test_empty_navigation_returns_empty_set(self):
        client = _make_client()
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=_navigation_response([]))

        assert await client.get_enabled_apps() == set()

    async def test_skips_entries_missing_both_keys(self):
        client = _make_client()
        client._client = AsyncMock()
        client._client.get = AsyncMock(
            return_value=_navigation_response(
                [{"name": "Logout", "href": "/logout"}, {"app": "notes"}]
            )
        )

        assert await client.get_enabled_apps() == {"notes"}

    @pytest.mark.parametrize("body", [{}, {"ocs": None}, {"ocs": {"data": None}}])
    async def test_malformed_envelope_returns_empty_set(self, body):
        """A missing/null ``ocs``/``data`` envelope yields an empty set rather
        than raising. NOTE: an empty set does NOT trigger the
        ``_get_enabled_apps_or_none`` scan-all fallback (that fires only on
        exceptions) — all optional apps are gated off for this scan cycle, with
        Files unaffected (unconditional) and the next cycle retrying normally."""
        client = _make_client()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = body
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=response)

        assert await client.get_enabled_apps() == set()

    async def test_ocs_failure_status_raises(self):
        """A 200 with ``ocs.meta.status == "failure"`` raises so the scanner's
        ``_get_enabled_apps_or_none`` falls back to scanning all apps, instead
        of silently gating every app off on the empty ``data`` of a failure
        envelope."""
        client = _make_client()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "ocs": {"meta": {"status": "failure", "statuscode": 997}, "data": None}
        }
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=response)

        with pytest.raises(ValueError, match="failure"):
            await client.get_enabled_apps()


class TestNormaliseSearchResult:
    def test_adds_leading_slash_to_path(self):
        result = _normalise_search_result(
            {"path": "Documents/foo.pdf", "file_id": 1, "is_directory": False}
        )
        assert result["path"] == "/Documents/foo.pdf"

    def test_preserves_leading_slash_when_present(self):
        result = _normalise_search_result(
            {"path": "/Documents/foo.pdf", "file_id": 1, "is_directory": False}
        )
        assert result["path"] == "/Documents/foo.pdf"

    def test_maps_file_id_to_id(self):
        result = _normalise_search_result(
            {"path": "/foo.pdf", "file_id": 99, "is_directory": False}
        )
        assert result["id"] == 99

    def test_falls_back_to_id_when_file_id_missing(self):
        result = _normalise_search_result(
            {"path": "/foo.pdf", "id": 7, "is_directory": False}
        )
        assert result["id"] == 7

    def test_computes_last_modified_timestamp(self):
        result = _normalise_search_result(
            {
                "path": "/foo.pdf",
                "file_id": 1,
                "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            }
        )
        assert result["last_modified_timestamp"] == 1735689600

    def test_preserves_existing_timestamp(self):
        result = _normalise_search_result(
            {
                "path": "/foo.pdf",
                "file_id": 1,
                "last_modified_timestamp": 12345,
                "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            }
        )
        assert result["last_modified_timestamp"] == 12345

    def test_handles_unparseable_last_modified(self):
        result = _normalise_search_result(
            {"path": "/foo.pdf", "file_id": 1, "last_modified": "not-a-date"}
        )
        assert result["last_modified_timestamp"] is None


class TestFindFilesByTag:
    async def test_returns_empty_when_tag_missing(self):
        client = _make_client()
        client.webdav.get_tag_by_name = AsyncMock(return_value=None)

        result = await client.find_files_by_tag("does-not-exist")

        assert result == []
        client.webdav.get_files_by_tag.assert_not_called()

    async def test_returns_empty_when_no_tagged_items(self):
        client = _make_client()
        client.webdav.get_tag_by_name = AsyncMock(return_value={"id": 5})
        client.webdav.get_files_by_tag = AsyncMock(return_value=[])

        result = await client.find_files_by_tag("vector-index")

        assert result == []
        client.webdav.find_all_by_type.assert_not_called()

    async def test_directly_tagged_files_pass_through_with_mime_filter(self):
        client = _make_client()
        client.webdav.get_tag_by_name = AsyncMock(return_value={"id": 5})
        client.webdav.get_files_by_tag = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "path": "/Documents/a.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                },
                {
                    "id": 2,
                    "path": "/Documents/notes.md",
                    "content_type": "text/markdown",
                    "is_directory": False,
                },
            ]
        )

        result = await client.find_files_by_tag(
            "vector-index", mime_type_filter="application/pdf"
        )

        assert {f["id"] for f in result} == {1}
        # No tagged dirs → no SEARCH walk.
        client.webdav.find_all_by_type.assert_not_called()

    async def test_expands_tagged_directory_into_pdf_descendants(self):
        client = _make_client()
        client.webdav.get_tag_by_name = AsyncMock(return_value={"id": 5})
        # One directly-tagged folder, no directly-tagged files.
        client.webdav.get_files_by_tag = AsyncMock(
            return_value=[
                {
                    "id": 100,
                    "path": "/corpus",
                    "content_type": "httpd/unix-directory",
                    "is_directory": True,
                }
            ]
        )
        # Search inside the folder returns two PDFs.
        client.webdav.find_all_by_type = AsyncMock(
            return_value=[
                {
                    "file_id": 11,
                    "path": "corpus/arxiv/a.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                    "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                },
                {
                    "file_id": 12,
                    "path": "corpus/arxiv/b.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                    "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                },
            ]
        )

        result = await client.find_files_by_tag(
            "vector-index", mime_type_filter="application/pdf"
        )

        assert {f["id"] for f in result} == {11, 12}
        # Each result is normalised to the get_files_by_tag shape.
        for f in result:
            assert f["path"].startswith("/")
            assert f["last_modified_timestamp"] is not None
        # SEARCH was scoped to the tagged folder (no leading slash) and
        # forwarded the requested MIME type as the positional first arg.
        client.webdav.find_all_by_type.assert_awaited_once()
        call_args = client.webdav.find_all_by_type.await_args
        assert call_args.args[0] == "application/pdf"
        assert call_args.kwargs["scope"] == "corpus"

    async def test_dedupes_when_file_directly_tagged_and_under_tagged_folder(self):
        client = _make_client()
        client.webdav.get_tag_by_name = AsyncMock(return_value={"id": 5})
        client.webdav.get_files_by_tag = AsyncMock(
            return_value=[
                {
                    "id": 11,
                    "path": "/corpus/arxiv/a.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                    "name": "a.pdf",
                },
                {
                    "id": 100,
                    "path": "/corpus",
                    "content_type": "httpd/unix-directory",
                    "is_directory": True,
                },
            ]
        )
        client.webdav.find_all_by_type = AsyncMock(
            return_value=[
                {
                    "file_id": 11,
                    "path": "corpus/arxiv/a.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                },
                {
                    "file_id": 12,
                    "path": "corpus/arxiv/b.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                },
            ]
        )

        result = await client.find_files_by_tag(
            "vector-index", mime_type_filter="application/pdf"
        )

        # File 11 is included exactly once and keeps the directly-tagged
        # entry's metadata (name from get_files_by_tag, not search).
        assert sorted(f["id"] for f in result) == [11, 12]
        assert next(f for f in result if f["id"] == 11)["name"] == "a.pdf"

    async def test_directory_walk_failure_skips_only_that_directory(self, caplog):
        client = _make_client()
        client.webdav.get_tag_by_name = AsyncMock(return_value={"id": 5})
        client.webdav.get_files_by_tag = AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "path": "/Documents/keep.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                },
                {
                    "id": 100,
                    "path": "/broken",
                    "content_type": "httpd/unix-directory",
                    "is_directory": True,
                },
            ]
        )
        client.webdav.find_all_by_type = AsyncMock(
            side_effect=RuntimeError("REPORT 500")
        )

        import logging

        caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.client")
        result = await client.find_files_by_tag(
            "vector-index", mime_type_filter="application/pdf"
        )

        # Directly-tagged file survives even though the dir walk blew up.
        assert {f["id"] for f in result} == {7}
        assert "Tag-based directory walk failed" in caplog.text

    async def test_no_mime_filter_skips_directory_expansion(self):
        client = _make_client()
        client.webdav.get_tag_by_name = AsyncMock(return_value={"id": 5})
        client.webdav.get_files_by_tag = AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "path": "/Documents/keep.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                },
                {
                    "id": 100,
                    "path": "/corpus",
                    "content_type": "httpd/unix-directory",
                    "is_directory": True,
                },
            ]
        )

        result = await client.find_files_by_tag("vector-index")

        # Without a MIME filter, directory expansion would fan out
        # uncontrollably — the helper deliberately skips it.
        assert {f["id"] for f in result} == {7}
        client.webdav.find_all_by_type.assert_not_called()

    async def test_skips_descendant_directories_in_search_results(self):
        """find_all_by_type can return collections too (e.g. when the SEARCH
        backend treats a folder's mime type as matching). Those must not
        slip through and clobber file IDs."""
        client = _make_client()
        client.webdav.get_tag_by_name = AsyncMock(return_value={"id": 5})
        client.webdav.get_files_by_tag = AsyncMock(
            return_value=[
                {
                    "id": 100,
                    "path": "/corpus",
                    "content_type": "httpd/unix-directory",
                    "is_directory": True,
                }
            ]
        )
        client.webdav.find_all_by_type = AsyncMock(
            return_value=[
                {
                    "file_id": 50,
                    "path": "corpus/sub",
                    "content_type": "httpd/unix-directory",
                    "is_directory": True,
                },
                {
                    "file_id": 51,
                    "path": "corpus/sub/a.pdf",
                    "content_type": "application/pdf",
                    "is_directory": False,
                },
            ]
        )

        result = await client.find_files_by_tag(
            "vector-index", mime_type_filter="application/pdf"
        )

        assert {f["id"] for f in result} == {51}
