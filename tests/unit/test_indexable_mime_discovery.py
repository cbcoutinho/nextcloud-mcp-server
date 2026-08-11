"""Tagged-file discovery covers the office formats, not only PDFs."""

import anyio
import pytest

from nextcloud_mcp_server.client import _as_mime_tuple
from nextcloud_mcp_server.config import Settings

pytestmark = pytest.mark.unit

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestMimeTupleNormalisation:
    def test_a_bare_string_is_wrapped_not_iterated(self):
        """Iterating it would make startswith match on single letters."""
        assert _as_mime_tuple("application/pdf") == ("application/pdf",)

    def test_none_means_no_filter(self):
        assert _as_mime_tuple(None) == ()

    def test_a_sequence_is_preserved_in_order(self):
        assert _as_mime_tuple([DOCX, XLSX]) == (DOCX, XLSX)

    def test_the_result_drives_startswith_directly(self):
        types = _as_mime_tuple(["application/pdf", DOCX])

        assert "application/pdf".startswith(types)
        assert f"{DOCX}; charset=binary".startswith(types)
        assert not "image/png".startswith(types)


class TestTaggedFolderExpansion:
    """A tagged folder needs one SEARCH per type; they must not be sequential."""

    @staticmethod
    def _client(
        mocker, per_type: dict[str, list[dict]] | None = None, fail: set | None = None
    ):
        from nextcloud_mcp_server.client import NextcloudClient

        client = mocker.Mock(spec=NextcloudClient)
        in_flight = 0
        peak = 0

        async def find_all_by_type(mime_type, scope=None):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await anyio.sleep(0.01)
            in_flight -= 1
            if fail and mime_type in fail:
                raise RuntimeError(f"SEARCH failed for {mime_type}")
            return (per_type or {}).get(mime_type, [])

        client.webdav = mocker.Mock()
        client.webdav.find_all_by_type = find_all_by_type
        client._walk_tagged_dir = NextcloudClient._walk_tagged_dir.__get__(client)
        client.peak = lambda: peak
        return client

    async def test_the_per_type_searches_run_concurrently(self, mocker):
        client = self._client(mocker)

        await client._walk_tagged_dir("/Docs", (DOCX, XLSX, "application/pdf"), "t")

        assert client.peak() == 3, (
            f"expected the 3 per-type SEARCHes to overlap, saw {client.peak()}"
        )

    async def test_results_are_ordered_by_type_not_by_completion(self, mocker):
        """Deterministic discovery output for the same corpus."""
        client = self._client(
            mocker,
            per_type={
                DOCX: [{"id": 1}],
                XLSX: [{"id": 2}],
                "application/pdf": [{"id": 3}],
            },
        )

        found, failed = await client._walk_tagged_dir(
            "/Docs", (DOCX, XLSX, "application/pdf"), "t"
        )

        assert [row["id"] for row in found] == [1, 2, 3]
        assert failed is False

    async def test_one_failing_type_keeps_the_others(self, mocker):
        """A partial folder beats an empty one."""
        client = self._client(
            mocker,
            per_type={DOCX: [{"id": 1}], "application/pdf": [{"id": 3}]},
            fail={XLSX},
        )

        found, failed = await client._walk_tagged_dir(
            "/Docs", (DOCX, XLSX, "application/pdf"), "t"
        )

        assert [row["id"] for row in found] == [1, 3]
        assert failed is True


class TestDirectlyTaggedFiles:
    """The non-folder path: a tagged file is filtered by its own content type.

    `_as_mime_tuple`'s tests cover `startswith(tuple)` in isolation, but nothing
    exercised the line in `find_files_by_tag` that applies it to a directly
    tagged file -- which is how most office documents will actually be tagged.
    """

    @staticmethod
    def _client(mocker, items):
        from nextcloud_mcp_server.client import NextcloudClient

        client = mocker.Mock(spec=NextcloudClient)
        client.webdav = mocker.Mock()
        client.webdav.get_tag_by_name = mocker.AsyncMock(return_value={"id": 7})
        client.webdav.get_files_by_tag = mocker.AsyncMock(return_value=items)
        client.find_files_by_tag = NextcloudClient.find_files_by_tag.__get__(client)
        return client

    async def test_office_files_pass_and_others_are_dropped(self, mocker):
        client = self._client(
            mocker,
            [
                {"id": 1, "content_type": DOCX, "path": "/a.docx"},
                {"id": 2, "content_type": XLSX, "path": "/b.xlsx"},
                {"id": 3, "content_type": "application/pdf", "path": "/c.pdf"},
                {"id": 4, "content_type": "image/png", "path": "/d.png"},
                {"id": 5, "content_type": "text/plain", "path": "/e.txt"},
            ],
        )

        found = await client.find_files_by_tag(
            "vector-index", mime_type_filter=Settings().indexable_mime_types
        )

        assert sorted(f["id"] for f in found) == [1, 2, 3]

    async def test_a_content_type_with_parameters_still_matches(self, mocker):
        """WebDAV may report `…document; charset=binary`; startswith handles it."""
        client = self._client(
            mocker,
            [{"id": 1, "content_type": f"{DOCX}; charset=binary", "path": "/a.docx"}],
        )

        found = await client.find_files_by_tag(
            "vector-index", mime_type_filter=Settings().indexable_mime_types
        )

        assert [f["id"] for f in found] == [1]


class TestEmptyAllowlist:
    """An emptied allowlist means "index nothing", not "no filter".

    Passed straight through, an empty tuple did neither consistently: the
    directly-tagged content-type test was skipped (indexing every type) while
    tagged-folder expansion was skipped too (indexing none) — the opposite of
    what an allowlist is for, and reachable by an operator following the
    "set empty to disable" convention `vector_sync_keyword_tag` establishes.
    """

    @staticmethod
    def _settings(mocker):
        settings = mocker.MagicMock()
        settings.vector_sync_tag = "vector-index"
        settings.vector_sync_keyword_tag = ""
        settings.indexable_mime_types = ()
        return settings

    async def test_nothing_is_discovered_and_the_client_is_not_called(self, mocker):
        from nextcloud_mcp_server.vector.scanner import _discover_tagged_files

        nc = mocker.MagicMock()
        nc.find_files_by_tag = mocker.AsyncMock(
            side_effect=AssertionError("discovery must not query with an empty list")
        )

        assert await _discover_tagged_files(nc, self._settings(mocker)) == []
        nc.find_files_by_tag.assert_not_awaited()

    async def test_it_warns_so_the_silence_is_explicable(self, mocker, caplog):
        from nextcloud_mcp_server.vector.scanner import _discover_tagged_files

        nc = mocker.MagicMock()
        nc.find_files_by_tag = mocker.AsyncMock(return_value=[])

        with caplog.at_level("WARNING"):
            await _discover_tagged_files(nc, self._settings(mocker))

        assert "VECTOR_SYNC_INDEXABLE_MIME_TYPES is empty" in caplog.text


class TestIndexableSetting:
    def test_the_default_covers_pdf_office_and_outlook(self):
        types = Settings().indexable_mime_types

        assert "application/pdf" in types
        assert "application/msword" in types
        assert DOCX in types
        assert "application/vnd.ms-excel" in types
        assert XLSX in types
        assert "application/vnd.ms-outlook" in types

    def test_whitespace_and_blank_entries_are_dropped(self):
        settings = Settings(
            vector_sync_indexable_mime_types="application/pdf, ,  text/plain,"
        )

        assert settings.indexable_mime_types == ("application/pdf", "text/plain")

    def test_an_operator_can_narrow_it_back_to_pdf_only(self):
        settings = Settings(vector_sync_indexable_mime_types="application/pdf")

        assert settings.indexable_mime_types == ("application/pdf",)
