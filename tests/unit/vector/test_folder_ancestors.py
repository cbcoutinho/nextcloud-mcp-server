"""Unit tests for folder-ancestor resolution (ADR-033 Phase 3, Deck #740)."""

from __future__ import annotations

import pytest

from nextcloud_mcp_server.vector.folder_ancestors import (
    ancestor_dir_paths,
    resolve_folder_ancestors,
)

pytestmark = pytest.mark.unit


class _StubWebdav:
    """Resolves folder paths to fileids from a fixed map; counts calls."""

    def __init__(self, mapping: dict[str, str | None], *, raises: set | None = None):
        self._mapping = mapping
        self._raises = raises or set()
        self.calls: list[str] = []

    async def get_fileid(self, path: str) -> str | None:
        self.calls.append(path)
        if path in self._raises:
            raise RuntimeError("boom")
        return self._mapping.get(path)


class TestAncestorDirPaths:
    def test_immediate_parent_first_root_excluded(self) -> None:
        assert ancestor_dir_paths("/A/B/doc.pdf") == ["/A/B", "/A"]

    def test_top_level_file_has_no_ancestors(self) -> None:
        # Only the root would be an ancestor, and root is excluded.
        assert ancestor_dir_paths("/doc.pdf") == []

    def test_empty_path(self) -> None:
        assert ancestor_dir_paths("") == []


class TestResolveFolderAncestors:
    async def test_resolves_each_ancestor_in_order(self) -> None:
        webdav = _StubWebdav({"/A/B": "200", "/A": "100"})
        got = await resolve_folder_ancestors(webdav, "/A/B/doc.pdf")
        # Immediate parent first.
        assert got == ["200", "100"]

    async def test_unresolved_ancestor_is_skipped(self) -> None:
        # /A/B has no fileid (None) -> omitted; /A resolves.
        webdav = _StubWebdav({"/A/B": None, "/A": "100"})
        got = await resolve_folder_ancestors(webdav, "/A/B/doc.pdf")
        assert got == ["100"]

    async def test_error_on_one_ancestor_is_best_effort(self) -> None:
        webdav = _StubWebdav({"/A": "100"}, raises={"/A/B"})
        got = await resolve_folder_ancestors(webdav, "/A/B/doc.pdf")
        assert got == ["100"]  # the raising folder is skipped, not fatal

    async def test_empty_path_returns_empty_without_calls(self) -> None:
        webdav = _StubWebdav({})
        assert await resolve_folder_ancestors(webdav, "") == []
        assert webdav.calls == []

    async def test_cache_memoises_repeated_lookups(self) -> None:
        webdav = _StubWebdav({"/A/B": "200", "/A": "100"})
        cache: dict[str, str | None] = {}
        await resolve_folder_ancestors(webdav, "/A/B/x.pdf", cache=cache)
        await resolve_folder_ancestors(webdav, "/A/B/y.pdf", cache=cache)
        # Two files under the same folders -> each folder resolved once.
        assert sorted(webdav.calls) == ["/A", "/A/B"]
        assert cache == {"/A/B": "200", "/A": "100"}
