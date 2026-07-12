"""Unit tests for the Pyroscope profiling setup gating.

Only the no-op paths are exercised here — they never import pyroscope-io or
start the background profiler thread, so the tests stay fast and side-effect
free. The enabled+configured path is covered end-to-end at deploy time.
"""

import logging
from unittest.mock import patch

import pytest

from nextcloud_mcp_server.observability import profiling

pytestmark = pytest.mark.unit

# Opaque server-address fixture. Never dialed — the enabled tests mock
# pyroscope.configure and the disabled/no-server tests return early — so it is
# left scheme-less (no clear-text-protocol literal for a scanner to flag).
SERVER = "alloy.alloy.svc.cluster.local:4041"


def _reset():
    profiling._configured = False


def test_setup_profiling_noop_when_disabled():
    _reset()
    profiling.setup_profiling("nextcloud-mcp-server-api", SERVER, enabled=False)
    assert profiling._configured is False


def test_setup_profiling_noop_when_server_unset(caplog):
    _reset()
    with caplog.at_level(logging.WARNING, logger=profiling.logger.name):
        profiling.setup_profiling("nextcloud-mcp-server-worker", None, enabled=True)
    assert profiling._configured is False
    assert "PYROSCOPE_SERVER_ADDRESS" in caplog.text


def test_setup_profiling_configures_when_enabled():
    """Enabled + server address → pyroscope.configure() called with the exact
    kwargs. Guards against a wrong/renamed kwarg against the pinned pyroscope-io
    API (e.g. tags=) that would otherwise only surface at deploy time.
    """
    # pyroscope-io is an optional extra (not in the default deps); skip when the
    # runtime SDK isn't installed rather than fail.
    pytest.importorskip("pyroscope")
    _reset()
    with patch("pyroscope.configure") as mock_configure:
        profiling.setup_profiling(
            "nextcloud-mcp-server-worker",
            SERVER,
            enabled=True,
            tags={"role": "worker"},
        )
    assert profiling._configured is True
    mock_configure.assert_called_once_with(
        application_name="nextcloud-mcp-server-worker",
        server_address=SERVER,
        tags={"role": "worker"},
    )


def test_setup_profiling_idempotent():
    """A second call is a no-op once configured (does not re-call configure)."""
    pytest.importorskip("pyroscope")
    _reset()
    with patch("pyroscope.configure") as mock_configure:
        profiling.setup_profiling("svc-a", SERVER, enabled=True)
        profiling.setup_profiling("svc-b", SERVER, enabled=True)
    assert mock_configure.call_count == 1


def test_setup_profiling_degrades_on_configure_error(caplog):
    """A pyroscope.configure() failure must not propagate (fail open)."""
    pytest.importorskip("pyroscope")
    _reset()
    with (
        patch("pyroscope.configure", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.WARNING, logger=profiling.logger.name),
    ):
        profiling.setup_profiling("svc", SERVER, enabled=True)  # must not raise
    assert profiling._configured is False
    assert "failed to configure" in caplog.text
