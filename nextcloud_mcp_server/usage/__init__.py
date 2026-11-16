"""Per-tenant usage metering (data plane).

Records billable operations into the app-DB ``usage_events`` table for the
control plane to pull. Gated by ``USAGE_METERING_ENABLED`` (default off). See
Deck #67 and control-plane ``usage-metering.md``.
"""

from nextcloud_mcp_server.usage.store import (
    UsageEvent,
    UsageEventStore,
    embedding_token_events,
)

__all__ = ["UsageEvent", "UsageEventStore", "embedding_token_events"]
