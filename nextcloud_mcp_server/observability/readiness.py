"""Non-blocking readiness dependency-health cache.

Kubernetes readiness probes must be cheap and must not gate a (typically
single-replica) tenant Pod out of its Service on transient external-dependency
latency. Doing so converts a *degraded* shared dependency (Nextcloud, Qdrant)
into a *total* outage: the only Pod is removed from the Service, the gateway
has no upstream, and connected MCP clients see their streamable-HTTP sessions
drop and fail to reconnect (Deck #302).

A background loop refreshes this snapshot off the probe path; the readiness
handler only ever reads ``snapshot()`` (no I/O), so probe latency is decoupled
from upstream latency. Dependency results are reported for observability but are
intentionally *non-gating*.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field


class DependencyStatus(BaseModel):
    """Last observed health of a single external dependency.

    ``healthy`` is ``None`` until the first check completes; ``detail`` carries
    the human-readable string the readiness handler reports verbatim
    (``"ok"`` / ``"embedded"`` / ``"pending"`` / ``"error: ..."``).
    """

    name: str
    healthy: bool | None = None
    detail: str = "pending"
    checked_at: float = 0.0


class ReadinessCache(BaseModel):
    """Time-bounded snapshot of external dependency health.

    Written only by the background refresh loop and read only by the readiness
    handler. The single-writer invariant (one refresh loop) is what makes this
    safe without a lock; a reader simply tolerates seeing the previous value for
    one entry until the next refresh.
    """

    ttl_seconds: float = 30.0
    statuses: dict[str, DependencyStatus] = Field(default_factory=dict)

    def snapshot(self) -> dict[str, DependencyStatus]:
        """Return a shallow copy of the current per-dependency statuses."""
        return dict(self.statuses)

    def update(
        self, name: str, healthy: bool, detail: str, *, now: float | None = None
    ) -> None:
        """Record the outcome of a dependency check."""
        self.statuses[name] = DependencyStatus(
            name=name,
            healthy=healthy,
            detail=detail,
            checked_at=time.monotonic() if now is None else now,
        )

    def is_stale(self, *, now: float | None = None) -> bool:
        """True when there is no data yet or any entry is older than the TTL.

        Exposed for observability/diagnostics; the refresh loop runs on a fixed
        cadence rather than polling this.
        """
        if not self.statuses:
            return True
        current = time.monotonic() if now is None else now
        # Inclusive boundary: exactly ttl_seconds old counts as stale.
        return any(
            current - status.checked_at >= self.ttl_seconds
            for status in self.statuses.values()
        )
