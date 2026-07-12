"""Continuous profiling (Grafana Pyroscope) setup.

Push-mode via the Pyroscope SDK (``pyroscope-io``). The process periodically
pushes CPU/wall profiles to an Alloy ``pyroscope.receive_http`` endpoint
(``server_address``), which forwards them to the homelab Pyroscope backend.

No-op unless explicitly enabled and a server address is configured, so it is
safe to import and call unconditionally from the API and worker entrypoints.
The ``cluster`` label is stamped downstream by Alloy's ``pyroscope.write``
external_labels, so it is intentionally not set here.
"""

import logging

logger = logging.getLogger(__name__)

_configured = False


def setup_profiling(
    application_name: str,
    server_address: str | None,
    *,
    enabled: bool = False,
    tags: dict[str, str] | None = None,
) -> None:
    """Configure Pyroscope push-mode profiling if enabled.

    Args:
        application_name: Pyroscope application name (e.g.
            ``nextcloud-mcp-server-worker``). Distinguishes api vs worker.
        server_address: Alloy pyroscope.receive_http URL (e.g.
            ``http://alloy.alloy.svc.cluster.local:4041``). Required when enabled.
        enabled: Master switch (``PYROSCOPE_ENABLED``). No-op when False.
        tags: Optional extra tags to attach to every profile.

    Idempotent: only the first successful call per process takes effect.
    """
    global _configured
    if _configured:
        logger.debug(
            "Pyroscope profiling already configured; ignoring repeat call "
            "(application=%s)",
            application_name,
        )
        return
    if not enabled:
        logger.debug("Pyroscope profiling disabled")
        return
    if not server_address:
        logger.warning(
            "Pyroscope profiling enabled but PYROSCOPE_SERVER_ADDRESS is unset; "
            "skipping profiler setup"
        )
        return

    try:
        # pyroscope-io is an optional dependency; import lazily so a missing
        # install degrades to a warning instead of a startup ImportError.
        import pyroscope  # noqa: PLC0415
    except ImportError:
        logger.warning("pyroscope-io is not installed; continuous profiling disabled")
        return

    try:
        pyroscope.configure(
            application_name=application_name,
            server_address=server_address,
            tags=tags or {},
        )
    except Exception:  # noqa: BLE001 - profiling is optional; never crash startup
        # Fail open, matching setup_tracing()'s defensive OTLP-exporter handling:
        # a bad server_address / SDK error disables profiling rather than taking
        # down the API/worker process.
        logger.warning(
            "Pyroscope profiling failed to configure (application=%s); "
            "continuing without it",
            application_name,
            exc_info=True,
        )
        return

    _configured = True
    logger.info(
        "Pyroscope profiling enabled (application=%s, server=%s)",
        application_name,
        server_address,
    )
