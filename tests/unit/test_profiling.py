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


def test_setup_profiling_configures_when_enabled(monkeypatch):
    """Enabled + server address → pyroscope.configure() called with the exact
    kwargs. Guards against a wrong/renamed kwarg against the pinned pyroscope-io
    API (e.g. tags=) that would otherwise only surface at deploy time.
    """
    # pyroscope-io is an optional extra (not in the default deps); skip when the
    # runtime SDK isn't installed rather than fail.
    pytest.importorskip("pyroscope")
    _reset()
    # No downward API → no pod identity tags, so the assertion below stays exact
    # even when the test host happens to export these.
    _patch_pod_identity(monkeypatch)
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


def _patch_pod_identity(monkeypatch, *, namespace=None, pod=None):
    """Override the downward-API settings the profiler reads.

    Patched as class-level ``property`` objects: Settings is a dataclass, so a
    plain class attribute would lose to the instance's own value. A property is
    a data descriptor and wins.
    """
    from nextcloud_mcp_server.config import get_settings

    cls = type(get_settings())
    monkeypatch.setattr(cls, "pod_namespace", property(lambda self: namespace))
    monkeypatch.setattr(cls, "pod_name", property(lambda self: pod))


def test_pod_identity_tags_absent_without_downward_api(monkeypatch):
    """Self-hosted deployments must not gain empty namespace/pod labels."""
    _patch_pod_identity(monkeypatch)
    assert profiling._pod_identity_tags() == {}


def test_pod_identity_tags_skip_blank_values(monkeypatch):
    """A var set but empty is treated as absent, not as an empty label."""
    _patch_pod_identity(monkeypatch, namespace="   ", pod="backend-abc")
    assert profiling._pod_identity_tags() == {"pod": "backend-abc"}


def test_setup_profiling_tags_include_pod_identity(monkeypatch):
    """Deck #48: profiles must be attributable to a tenant by namespace.

    Without these every tenant's pod pushes under the same application_name and
    collapses into one unattributable series.
    """
    pytest.importorskip("pyroscope")
    _reset()
    _patch_pod_identity(
        monkeypatch, namespace="tenant-example", pod="backend-65df7d54-mtrtn"
    )
    with patch("pyroscope.configure") as mock_configure:
        profiling.setup_profiling("nextcloud-mcp-server-api", SERVER, enabled=True)
    assert mock_configure.call_args.kwargs["tags"] == {
        "namespace": "tenant-example",
        "pod": "backend-65df7d54-mtrtn",
    }


def test_setup_profiling_explicit_tag_overrides_pod_identity(monkeypatch):
    pytest.importorskip("pyroscope")
    _reset()
    _patch_pod_identity(monkeypatch, namespace="tenant-example")
    with patch("pyroscope.configure") as mock_configure:
        profiling.setup_profiling(
            "svc", SERVER, enabled=True, tags={"namespace": "override"}
        )
    assert mock_configure.call_args.kwargs["tags"] == {"namespace": "override"}


def test_shutdown_profiling_noop_when_not_configured():
    """Nothing to shed → False, and pyroscope is never touched."""
    _reset()
    assert profiling.shutdown_profiling() is False


def test_shutdown_profiling_stops_running_profiler():
    """The worker sheds the profiler rather than CrashLooping (Deck #908)."""
    pytest.importorskip("pyroscope")
    _reset()
    profiling._configured = True
    with patch("pyroscope.shutdown") as mock_shutdown:
        assert profiling.shutdown_profiling() is True
    mock_shutdown.assert_called_once_with()
    assert profiling._configured is False


def test_shutdown_profiling_never_raises(caplog):
    """A failure to stop the profiler must not mask the caller's original error."""
    pytest.importorskip("pyroscope")
    _reset()
    profiling._configured = True
    with (
        patch("pyroscope.shutdown", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.WARNING, logger=profiling.logger.name),
    ):
        assert profiling.shutdown_profiling() is False  # must not raise
    assert "Failed to shut down" in caplog.text
