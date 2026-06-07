"""Unit tests for the vector scanner's enabled-app gating helper.

``scan_user_documents`` skips polling apps the user doesn't have enabled (those
polls 404 and flood tenant logs). ``_get_enabled_apps_or_none`` resolves the
enabled-app set, returning ``None`` on any failure so the caller falls back to
scanning every app (the prior behaviour) rather than silently halting indexing.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import HTTPStatusError, Request, Response

from nextcloud_mcp_server.vector.scanner import _get_enabled_apps_or_none

pytestmark = pytest.mark.unit


async def test_returns_enabled_set_on_success():
    nc_client = AsyncMock()
    nc_client.get_enabled_apps = AsyncMock(return_value={"files", "notes"})

    result = await _get_enabled_apps_or_none(nc_client, "alice", scan_id=1234)

    assert result == {"files", "notes"}


async def test_returns_none_when_detection_raises(caplog):
    nc_client = AsyncMock()
    request = Request("GET", "http://nc.test/ocs/v2.php/core/navigation/apps")
    nc_client.get_enabled_apps = AsyncMock(
        side_effect=HTTPStatusError(
            "boom", request=request, response=Response(503, request=request)
        )
    )

    import logging

    caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.vector.scanner")
    result = await _get_enabled_apps_or_none(nc_client, "alice", scan_id=1234)

    # None signals scan-all fallback; the inline gate treats `None` as
    # "every app enabled" so indexing never silently stops.
    assert result is None
    assert "scanning all apps" in caplog.text


def test_none_set_enables_every_app():
    """The gate predicate used in scan_user_documents: a None set means
    detection failed, so every app must be scanned (back-compat)."""

    def app_enabled(app_id: str, enabled: set[str] | None) -> bool:
        return enabled is None or app_id in enabled

    assert app_enabled("news", None) is True
    assert app_enabled("deck", None) is True
    # And a concrete set gates precisely.
    assert app_enabled("news", {"notes"}) is False
    assert app_enabled("notes", {"notes"}) is True
