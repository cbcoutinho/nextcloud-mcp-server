"""Unit tests for BaseNextcloudClient.

These cover the URL prefix logic that routes ``/apps/...`` calls through the
universal ``/index.php`` entry point so the server works on Nextcloud installs
without Pretty URLs (issue #732).
"""

import pytest

from nextcloud_mcp_server.client.base import BaseNextcloudClient

pytestmark = pytest.mark.unit


def test_apps_path_is_prefixed_with_index_php():
    """Bare ``/apps/<app>/...`` 404s on Nextcloud without Pretty URLs; the
    universal ``/index.php/apps/...`` form must be used instead.
    """
    assert (
        BaseNextcloudClient._resolve_url("/apps/notes/api/v1/notes")
        == "/index.php/apps/notes/api/v1/notes"
    )
    assert (
        BaseNextcloudClient._resolve_url("/apps/deck/api/v1.0/boards/1/stacks/2/cards")
        == "/index.php/apps/deck/api/v1.0/boards/1/stacks/2/cards"
    )


def test_bare_core_path_gets_index_php_prefix():
    """Core routes (``/core/preview.png``) use the same front controller as
    app routes, so without pretty URLs they need ``/index.php`` too."""
    assert (
        BaseNextcloudClient._resolve_url("/core/preview.png")
        == "/index.php/core/preview.png"
    )
    assert (
        BaseNextcloudClient._resolve_url("/index.php/core/preview.png")
        == "/index.php/core/preview.png"
    )


def test_remote_php_dav_unchanged():
    """WebDAV/CalDAV/CardDAV paths use a dedicated entry point and don't need
    rewriting — leave them alone so we don't break the working call sites.
    """
    assert (
        BaseNextcloudClient._resolve_url("/remote.php/dav/files/alice/foo.txt")
        == "/remote.php/dav/files/alice/foo.txt"
    )


def test_ocs_path_unchanged():
    """The ``ocs/v2.php`` prefix is also a dedicated entry point — no rewrite."""
    assert (
        BaseNextcloudClient._resolve_url("/ocs/v2.php/cloud/users")
        == "/ocs/v2.php/cloud/users"
    )


def test_already_prefixed_unchanged():
    """If a caller already passed ``/index.php/apps/...`` we must not double-prefix."""
    assert (
        BaseNextcloudClient._resolve_url("/index.php/apps/notes/api/v1/notes")
        == "/index.php/apps/notes/api/v1/notes"
    )


def test_absolute_url_unchanged():
    """Absolute URLs (full ``https://...``) are pass-through; only path-prefix
    matching is intentional, and an ``http://...`` URL doesn't start with
    ``/apps/``.
    """
    assert (
        BaseNextcloudClient._resolve_url("https://cloud.example.org/apps/notes")
        == "https://cloud.example.org/apps/notes"
    )


def test_empty_or_unrelated_paths_unchanged():
    """Defensive cases: empty strings, root, and non-apps paths must pass through."""
    assert BaseNextcloudClient._resolve_url("") == ""
    assert BaseNextcloudClient._resolve_url("/") == "/"
    assert BaseNextcloudClient._resolve_url("/status.php") == "/status.php"
