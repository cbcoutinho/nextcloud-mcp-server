"""Unit tests for tag-based file exclusion (issue #710)."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from nextcloud_mcp_server.config import _reload_config
from nextcloud_mcp_server.server.tag_exclusion import (
    _normalise_path,
    get_excluded_file_paths,
    get_excluded_tag_names,
    is_path_excluded,
)


class TestNormalisePath:
    @pytest.mark.unit
    def test_strips_leading_and_trailing_slash(self):
        assert _normalise_path("/foo/bar/") == "foo/bar"

    @pytest.mark.unit
    def test_unchanged_when_already_clean(self):
        assert _normalise_path("foo/bar") == "foo/bar"

    @pytest.mark.unit
    def test_empty_string(self):
        assert _normalise_path("") == ""


class TestIsPathExcluded:
    @pytest.mark.unit
    def test_empty_set_excludes_nothing(self):
        assert is_path_excluded("/anything", set()) is False

    @pytest.mark.unit
    def test_direct_match(self):
        assert is_path_excluded("/Secret.txt", {"Secret.txt"}) is True

    @pytest.mark.unit
    def test_path_argument_is_normalised(self):
        # The path argument is normalised before comparison; the excluded
        # set is expected to already contain normalised entries (it always
        # is, in practice, because get_excluded_file_paths builds it).
        assert is_path_excluded("/Secret.txt/", {"Secret.txt"}) is True

    @pytest.mark.unit
    def test_descendant_of_excluded_directory(self):
        assert is_path_excluded("/Private/notes.md", {"Private"}) is True
        assert is_path_excluded("/Private/sub/file.txt", {"Private"}) is True

    @pytest.mark.unit
    def test_unrelated_path_not_excluded(self):
        assert is_path_excluded("/Public/notes.md", {"Private"}) is False

    @pytest.mark.unit
    def test_shared_prefix_is_not_a_match(self):
        # 'foobar' must NOT be excluded just because 'foo' is.
        # This is the bug a naive `startswith(exc)` would have.
        assert is_path_excluded("/foobar/x", {"foo"}) is False
        assert is_path_excluded("/foobar", {"foo"}) is False

    @pytest.mark.unit
    def test_excluded_path_itself(self):
        # The excluded entry itself is excluded (not just its descendants).
        assert is_path_excluded("/Private", {"Private"}) is True


class TestGetExcludedTagNames:
    @pytest.mark.unit
    @patch.dict(os.environ, {"EXCLUDED_TAGS": ""}, clear=False)
    def test_empty_returns_empty_list(self):
        _reload_config()
        assert get_excluded_tag_names() == []

    @pytest.mark.unit
    @patch.dict(os.environ, {"EXCLUDED_TAGS": "secret"}, clear=False)
    def test_single_tag(self):
        _reload_config()
        assert get_excluded_tag_names() == ["secret"]

    @pytest.mark.unit
    @patch.dict(os.environ, {"EXCLUDED_TAGS": "  a , b , c "}, clear=False)
    def test_strips_whitespace_around_each_tag(self):
        _reload_config()
        assert get_excluded_tag_names() == ["a", "b", "c"]

    @pytest.mark.unit
    @patch.dict(os.environ, {"EXCLUDED_TAGS": "a,,b,"}, clear=False)
    def test_skips_empty_entries(self):
        _reload_config()
        assert get_excluded_tag_names() == ["a", "b"]


class TestGetExcludedFilePaths:
    @pytest.mark.unit
    async def test_returns_empty_set_when_feature_disabled(self, mocker):
        mocker.patch(
            "nextcloud_mcp_server.server.tag_exclusion.get_excluded_tag_names",
            return_value=[],
        )
        webdav = AsyncMock()
        result = await get_excluded_file_paths(webdav)
        assert result == set()
        webdav.get_tag_by_name.assert_not_called()

    @pytest.mark.unit
    async def test_skips_unknown_tag(self, mocker):
        mocker.patch(
            "nextcloud_mcp_server.server.tag_exclusion.get_excluded_tag_names",
            return_value=["does-not-exist"],
        )
        webdav = AsyncMock()
        webdav.get_tag_by_name = AsyncMock(return_value=None)

        result = await get_excluded_file_paths(webdav)

        assert result == set()
        webdav.get_tag_by_name.assert_awaited_once_with("does-not-exist")
        webdav.get_files_by_tag.assert_not_called()

    @pytest.mark.unit
    async def test_collects_paths_from_multiple_tags(self, mocker):
        mocker.patch(
            "nextcloud_mcp_server.server.tag_exclusion.get_excluded_tag_names",
            return_value=["secret", "no-ai"],
        )
        webdav = AsyncMock()
        webdav.get_tag_by_name = AsyncMock(
            side_effect=[
                {"id": 1, "name": "secret"},
                {"id": 2, "name": "no-ai"},
            ]
        )
        webdav.get_files_by_tag = AsyncMock(
            side_effect=[
                [
                    {"path": "/Secret.txt", "is_directory": False},
                    {"path": "/Private/", "is_directory": True},
                ],
                [
                    # Same dir under a second tag — set dedupes it.
                    {"path": "/Private", "is_directory": True},
                    {"path": "/Other/notes.md", "is_directory": False},
                ],
            ]
        )

        result = await get_excluded_file_paths(webdav)

        assert result == {"Secret.txt", "Private", "Other/notes.md"}
        assert webdav.get_files_by_tag.await_count == 2
