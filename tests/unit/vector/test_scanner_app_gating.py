"""Unit tests for the vector scanner's enabled-app gating helpers.

``scan_user_documents`` skips polling apps the user doesn't have enabled (those
polls 404 and flood tenant logs). ``_get_enabled_apps_or_none`` resolves the
enabled-app set, returning ``None`` on any failure so the caller falls back to
scanning every app (the prior behaviour) rather than silently halting indexing;
``_app_enabled`` is the gate predicate applied per app.
"""

import logging
from unittest.mock import AsyncMock

import pytest
from httpx import HTTPStatusError, Request, Response

from nextcloud_mcp_server.vector.scanner import (
    _app_enabled,
    _get_enabled_apps_or_none,
)

pytestmark = pytest.mark.unit


async def test_returns_enabled_set_on_success():
    nc_client = AsyncMock()
    nc_client.get_enabled_apps = AsyncMock(return_value={"files", "notes"})

    result = await _get_enabled_apps_or_none(nc_client, "alice", scan_id=1234)

    assert result == {"files", "notes"}


async def test_returns_none_when_detection_raises(caplog):
    nc_client = AsyncMock()
    request = Request("GET", "https://nc.test/ocs/v2.php/core/navigation/apps")
    nc_client.get_enabled_apps = AsyncMock(
        side_effect=HTTPStatusError(
            "boom", request=request, response=Response(503, request=request)
        )
    )

    caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.vector.scanner")
    result = await _get_enabled_apps_or_none(nc_client, "alice", scan_id=1234)

    # None signals scan-all fallback; _app_enabled treats `None` as
    # "every app enabled" so indexing never silently stops.
    assert result is None
    assert "scanning all apps" in caplog.text


async def test_value_error_from_ocs_failure_returns_none():
    """A ValueError (e.g. OCS meta.status=='failure' from get_enabled_apps)
    routes through the scan-all fallback like any other exception."""
    nc_client = AsyncMock()
    nc_client.get_enabled_apps = AsyncMock(
        side_effect=ValueError("OCS navigation returned status='failure'")
    )

    result = await _get_enabled_apps_or_none(nc_client, "alice", scan_id=1234)

    assert result is None


def test_none_set_enables_every_app():
    """A None set means detection failed, so every app must be scanned."""
    assert _app_enabled("news", None) is True
    assert _app_enabled("deck", None) is True


def test_concrete_set_gates_precisely():
    """A resolved set scans only the apps it contains."""
    enabled = {"notes", "files"}
    assert _app_enabled("notes", enabled) is True
    assert _app_enabled("news", enabled) is False
    assert _app_enabled("deck", enabled) is False
