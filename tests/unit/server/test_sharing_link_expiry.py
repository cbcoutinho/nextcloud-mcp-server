"""Unit tests for the nc_share_create_public_link helpers.

The side-effect-free logic is extracted into module-level helpers so it can be
tested without exercising the full MCP tool path (which needs a Context + an
authenticated client):

- ``_compute_link_expiry`` — day-rounding of the expiry. Nextcloud expires a
  public link at 00:00:00 on ``expireDate`` in the owner's timezone (the end of
  the day before ``expireDate``), so the date must round up a day to cover the
  requested window.
- ``_build_link_response`` — maps a raw OCS ``shareType=3`` payload into the
  ``PublicDownloadLinkResponse`` (field mapping, ``download_url`` construction,
  empty-url failure).
"""

from datetime import datetime, timezone

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.server.sharing import (
    _build_link_response,
    _compute_link_expiry,
)

pytestmark = pytest.mark.unit


def test_compute_link_expiry_rounds_date_up_a_day():
    """expireDate is the day *after* the target instant's date, and expires_at
    is the precise requested instant rendered as RFC3339 'Z'."""
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)

    expire_date, expires_at = _compute_link_expiry(30, now)

    # target = 12:30 on 2026-06-02 → expireDate rounds up to 2026-06-03, i.e. the
    # link is valid until 00:00 on 2026-06-03 (the end of the target's day).
    assert expire_date == "2026-06-03"
    assert expires_at == "2026-06-02T12:30:00Z"


def test_compute_link_expiry_crosses_midnight():
    """A window that pushes past midnight rounds to the day after the target."""
    now = datetime(2026, 6, 2, 23, 50, 0, tzinfo=timezone.utc)

    expire_date, expires_at = _compute_link_expiry(30, now)

    # target = 00:20 on 2026-06-03 → expireDate = 2026-06-04.
    assert expire_date == "2026-06-04"
    assert expires_at == "2026-06-03T00:20:00Z"


_nows = st.datetimes(timezones=st.just(timezone.utc))


@given(st.integers(max_value=0), _nows)
@example(0, datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc))
def test_compute_link_expiry_rejects_non_positive(minutes, now):
    """Non-positive durations are rejected — this tool never mints a permanent
    (non-expiring) public link."""
    with pytest.raises(ToolError, match="positive"):
        _compute_link_expiry(minutes, now)


@pytest.mark.xfail(
    strict=True,
    reason="expires_in_minutes is an unbounded int: a huge value overflows "
    "datetime and raises OverflowError, which mcp masks as a bare "
    "'Error executing tool' instead of a ToolError",
)
@given(st.integers(), _nows)
@example(10**10, datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc))
def test_compute_link_expiry_raises_only_tool_error(minutes, now):
    try:
        _compute_link_expiry(minutes, now)
    except ToolError:
        return


def test_build_link_response_maps_fields():
    """Raw OCS payload fields map onto the response, download_url = url +
    '/download', and the advisory expires_at is passed through verbatim."""
    share_data = {
        "id": "42",  # OCS may serialize the id as a string
        "url": "https://nc.example.com/s/abc123",
        "token": "abc123",
        "permissions": 1,
    }

    resp = _build_link_response(
        "/Receipts/receipt.jpg", share_data, "2026-06-02T12:30:00Z"
    )

    assert resp.path == "/Receipts/receipt.jpg"
    assert resp.share_id == 42
    assert resp.url == "https://nc.example.com/s/abc123"
    assert resp.download_url == "https://nc.example.com/s/abc123/download"
    assert resp.token == "abc123"
    assert resp.permissions == 1
    assert resp.expires_at == "2026-06-02T12:30:00Z"


def test_build_link_response_strips_trailing_slash():
    """A trailing slash on the share url does not produce a double slash."""
    share_data = {"id": 1, "url": "https://nc.example.com/s/tok/", "token": "tok"}

    resp = _build_link_response("/f.png", share_data, "2026-06-02T12:30:00Z")

    assert resp.download_url == "https://nc.example.com/s/tok/download"


@pytest.mark.parametrize("share_data", [{"id": 1, "url": ""}, {"id": 1}])
def test_build_link_response_raises_on_missing_url(share_data):
    """A payload without a usable url is a hard error, not a silent empty
    response — OCS always returns one for shareType=3.

    ToolError specifically, not a bare exception: mcp 2.x replaces an
    unanticipated exception's message with "Error executing tool <name>", so a
    RuntimeError here would tell the model the call failed and nothing else.
    """
    with pytest.raises(ToolError, match="no url"):
        _build_link_response("/f.png", share_data, "2026-06-02T12:30:00Z")
