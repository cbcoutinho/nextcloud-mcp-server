"""Tests for config.py path resolution helpers added in PR #707.

Covers:
- get_token_db_path() / is_ephemeral_token_db() — ephemeral tempfile default
  with TOKEN_STORAGE_DB override.
- _resolve_settings_files() — optional external settings file discovery,
  including the NEXTCLOUD_MCP_SETTINGS_FILE env var and its colocation
  semantics for .secrets.toml.
"""

import os
import tempfile
from pathlib import Path

import pytest

import nextcloud_mcp_server.config as cfg
from nextcloud_mcp_server.config import (
    _reload_config,
    _resolve_settings_files,
    get_token_db_path,
    is_ephemeral_token_db,
)


@pytest.fixture(autouse=True)
def _reset_ephemeral_state(monkeypatch):
    """Reset module-global ephemeral tempfile state between tests.

    get_token_db_path() memoizes its result in a module-level global and
    registers an atexit hook for cleanup. Tests need an isolated slate so
    assertions about "already allocated" vs "not yet" are meaningful.
    """
    old = cfg._ephemeral_db_path
    cfg._ephemeral_db_path = None
    monkeypatch.delenv("TOKEN_STORAGE_DB", raising=False)
    monkeypatch.delenv("NEXTCLOUD_MCP_SETTINGS_FILE", raising=False)
    _reload_config()
    yield
    if cfg._ephemeral_db_path and os.path.exists(cfg._ephemeral_db_path):
        os.unlink(cfg._ephemeral_db_path)
    cfg._ephemeral_db_path = old
    _reload_config()


class TestGetTokenDbPath:
    def test_explicit_env_var_returned(self, monkeypatch, tmp_path):
        target = tmp_path / "explicit.db"
        monkeypatch.setenv("TOKEN_STORAGE_DB", str(target))
        _reload_config()

        assert get_token_db_path() == str(target)
        # No tempfile should have been allocated since we took the explicit
        # branch.
        assert cfg._ephemeral_db_path is None

    def test_ephemeral_tempfile_when_unset(self):
        path = get_token_db_path()

        assert path.startswith(tempfile.gettempdir())
        assert f"nextcloud-mcp-tokens-{os.getpid()}-" in os.path.basename(path)
        assert path.endswith(".db")
        assert os.path.exists(path)

    def test_ephemeral_tempfile_is_memoized(self):
        first = get_token_db_path()
        second = get_token_db_path()

        assert first == second
        # Only the memoized file should exist — no stray siblings.
        parent = Path(first).parent
        matches = list(parent.glob(f"nextcloud-mcp-tokens-{os.getpid()}-*.db"))
        assert matches == [Path(first)]

    def test_is_ephemeral_token_db_detects_allocated_path(self):
        path = get_token_db_path()

        assert is_ephemeral_token_db(path) is True
        assert is_ephemeral_token_db("/some/other/path") is False

    def test_is_ephemeral_token_db_before_allocation(self):
        # The autouse fixture reset _ephemeral_db_path to None; do not call
        # get_token_db_path() first. Nothing is allocated yet.
        assert cfg._ephemeral_db_path is None
        assert is_ephemeral_token_db("/any/path") is False
        assert is_ephemeral_token_db("") is False

    def test_explicit_path_does_not_trigger_tempfile(self, monkeypatch, tmp_path):
        """Regression guard: the explicit branch must short-circuit cleanly."""
        monkeypatch.setenv("TOKEN_STORAGE_DB", str(tmp_path / "pinned.db"))
        _reload_config()

        get_token_db_path()
        # Nothing under the tempfile prefix should have been created.
        matches = list(
            Path(tempfile.gettempdir()).glob(f"nextcloud-mcp-tokens-{os.getpid()}-*.db")
        )
        assert matches == []


class TestResolveSettingsFiles:
    def test_empty_list_when_nothing_present(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert _resolve_settings_files() == []

    def test_picks_up_cwd_settings(self, monkeypatch, tmp_path):
        (tmp_path / "settings.toml").write_text("[default]\n")
        monkeypatch.chdir(tmp_path)

        result = _resolve_settings_files()
        assert str(tmp_path / "settings.toml") in result

    def test_picks_up_cwd_secrets(self, monkeypatch, tmp_path):
        (tmp_path / ".secrets.toml").write_text("[default]\n")
        monkeypatch.chdir(tmp_path)

        result = _resolve_settings_files()
        assert str(tmp_path / ".secrets.toml") in result

    def test_explicit_settings_file_included(self, monkeypatch, tmp_path):
        explicit = tmp_path / "nested" / "my-settings.toml"
        explicit.parent.mkdir()
        explicit.write_text("[default]\n")
        monkeypatch.setenv("NEXTCLOUD_MCP_SETTINGS_FILE", str(explicit))
        # cwd has no settings.toml / .secrets.toml
        monkeypatch.chdir(tmp_path)

        result = _resolve_settings_files()
        assert result == [str(explicit)]

    def test_explicit_settings_file_secrets_colocated(self, monkeypatch, tmp_path):
        """PR #707 reviewer feedback: .secrets.toml should live beside
        the explicit settings file, not always in cwd."""
        config_dir = tmp_path / "etc"
        config_dir.mkdir()
        explicit = config_dir / "settings.toml"
        explicit.write_text("[default]\n")
        secrets = config_dir / ".secrets.toml"
        secrets.write_text("[default]\n")

        monkeypatch.setenv("NEXTCLOUD_MCP_SETTINGS_FILE", str(explicit))
        # cwd is elsewhere and deliberately contains *no* secrets file
        monkeypatch.chdir(tmp_path)

        result = _resolve_settings_files()
        assert str(explicit) in result
        assert str(secrets) in result

    def test_explicit_missing_raises(self, monkeypatch, tmp_path):
        """PR #707 reviewer feedback: missing explicit path must not be
        silently ignored — users will think their config is applied when
        it isn't."""
        missing = tmp_path / "does-not-exist.toml"
        monkeypatch.setenv("NEXTCLOUD_MCP_SETTINGS_FILE", str(missing))
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError, match="does-not-exist.toml"):
            _resolve_settings_files()
