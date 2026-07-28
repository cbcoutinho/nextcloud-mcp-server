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


def _pod_identity_tags() -> dict[str, str]:
    """Pyroscope tags identifying which pod a profile came from.

    Every tenant's pod pushes under the same ``application_name``, so without
    these the whole fleet collapses into one unattributable series -- there is
    no per-tenant profile at all. Tenants are identified by their
    ``tenant-<slug>`` namespace, matching how metrics and logs are already
    attributed (Deck #48).

    ``POD_NAMESPACE``/``POD_NAME`` are injected by the chart from the Kubernetes
    downward API. Read through ``Settings`` (not ``os.environ``) like every
    other config in this repo, and resolved per call so test overrides apply.
    Blank/unset values are skipped so non-Kubernetes deployments do not gain
    empty labels.
    """
    from nextcloud_mcp_server.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    candidates = {"namespace": settings.pod_namespace, "pod": settings.pod_name}
    return {
        tag: value.strip()
        for tag, value in candidates.items()
        if value and value.strip()
    }


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
        tags: Optional extra tags to attach to every profile. Pod identity
            (``namespace``/``pod``, from the downward API) is added
            automatically; a tag passed here with the same name overrides it.

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

    # Pod identity first so an explicit caller tag of the same name wins.
    resolved_tags = {**_pod_identity_tags(), **(tags or {})}

    try:
        pyroscope.configure(
            application_name=application_name,
            server_address=server_address,
            tags=resolved_tags,
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
        "Pyroscope profiling enabled (application=%s, server=%s, tags=%s)",
        application_name,
        server_address,
        # Keys AND values: the whole point of this line is confirming a pod
        # actually picked up its namespace/pod identity, which the key alone
        # cannot tell you. Sorted for stable, diffable log output.
        dict(sorted(resolved_tags.items())),
    )


def shutdown_profiling() -> bool:
    """Stop the profiler if it is running. Returns True if it was shut down.

    Profiling is optional telemetry, so it must never be the reason a process
    cannot start. Callers use this to *shed* the profiler when startup fails
    with it enabled (see the ingest worker in ``cli.py``): a pod running with
    degraded telemetry beats a pod in CrashLoopBackOff processing nothing.

    Never raises — a failure to stop the profiler must not mask the original
    startup error the caller is recovering from.
    """
    global _configured
    if not _configured:
        return False
    try:
        import pyroscope  # noqa: PLC0415

        pyroscope.shutdown()
    except Exception:  # noqa: BLE001 - best-effort; see docstring
        logger.warning("Failed to shut down the Pyroscope profiler", exc_info=True)
        return False
    _configured = False
    logger.info("Pyroscope profiling shut down")
    return True
