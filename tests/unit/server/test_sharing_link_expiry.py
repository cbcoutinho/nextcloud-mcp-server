"""Unit tests for the public-link expiry computation in nc_share_create_public_link.

The day-rounding logic is extracted into ``_compute_link_expiry`` so it can be
tested deterministically (with an injected ``now``) without exercising the full
MCP tool path. Nextcloud expires public links at midnight (the *start*) of
``expireDate`` in the owner's timezone, so the date must round up a day to
guarantee the requested window is covered.
"""

from datetime import datetime, timezone

import pytest

from nextcloud_mcp_server.server.sharing import _compute_link_expiry

pytestmark = pytest.mark.unit


def test_compute_link_expiry_rounds_date_up_a_day():
    """expireDate is the day *after* the target instant's date, and expires_at
    is the precise requested instant rendered as RFC3339 'Z'."""
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)

    expire_date, expires_at = _compute_link_expiry(30, now)

    # target = 12:30 on 2026-06-02 → rounds up to 2026-06-03 (midnight = end of
    # the target's day server-side).
    assert expire_date == "2026-06-03"
    assert expires_at == "2026-06-02T12:30:00Z"


def test_compute_link_expiry_crosses_midnight():
    """A window that pushes past midnight rounds to the day after the target."""
    now = datetime(2026, 6, 2, 23, 50, 0, tzinfo=timezone.utc)

    expire_date, expires_at = _compute_link_expiry(30, now)

    # target = 00:20 on 2026-06-03 → expireDate = 2026-06-04.
    assert expire_date == "2026-06-04"
    assert expires_at == "2026-06-03T00:20:00Z"


@pytest.mark.parametrize("minutes", [0, -1, -60])
def test_compute_link_expiry_rejects_non_positive(minutes):
    """Non-positive durations are rejected — this tool never mints a permanent
    (non-expiring) public link."""
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="positive"):
        _compute_link_expiry(minutes, now)
