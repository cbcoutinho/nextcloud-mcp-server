"""Unit tests for the Pyroscope profiling setup gating.

Only the no-op paths are exercised here — they never import pyroscope-io or
start the background profiler thread, so the tests stay fast and side-effect
free. The enabled+configured path is covered end-to-end at deploy time.
"""

import logging

from nextcloud_mcp_server.observability import profiling


def _reset():
    profiling._configured = False


def test_setup_profiling_noop_when_disabled():
    _reset()
    profiling.setup_profiling(
        "nextcloud-mcp-server-api", "http://alloy.alloy.svc:4041", enabled=False
    )
    assert profiling._configured is False


def test_setup_profiling_noop_when_server_unset(caplog):
    _reset()
    with caplog.at_level(logging.WARNING, logger=profiling.logger.name):
        profiling.setup_profiling("nextcloud-mcp-server-worker", None, enabled=True)
    assert profiling._configured is False
    assert "PYROSCOPE_SERVER_ADDRESS" in caplog.text
