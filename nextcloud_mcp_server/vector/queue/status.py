"""Status surface for ingest jobs (design §10.1, ``STATUS_BACKEND``).

- ``local``: in-process job state — the memory-stream buffer (today's behavior,
  read directly by the status endpoint).
- ``bus``: a background subscriber consumes
  ``mcp.document.{ready,failed,reparsed}.{tenant_id}`` into a bounded in-process
  :class:`StatusStore` that the status endpoint / ``nc_get_vector_sync_status``
  read.

**Honest constraint (design §10.2 / decision):** MCP progress notifications
(``ctx.report_progress``) can only be emitted inside an *active tool-call
request*; a background subscriber has no ``ctx`` and the MCP SDK exposes no
out-of-band push. So "surface events as MCP progress notifications" is delivered
via this store (polled by the status endpoint / a tool), not an unsolicited
server push. True server-initiated progress / SSE is a follow-up — the
``on_event`` callback seam is left in place for it.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    import anyio
    from anyio.abc import TaskStatus

logger = logging.getLogger(__name__)

# Terminal/intermediate document states carried on mcp.document.* subjects.
_VALID_STATES = {"ready", "failed", "reparsed"}


class StatusStore:
    """Bounded LRU of recent document states keyed by ``doc_id``."""

    def __init__(self, max_size: int = 10_000):
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max = max_size

    def record(
        self,
        doc_id: str,
        state: str,
        *,
        content_hash: str | None = None,
        transitioned_at: str | None = None,
    ) -> None:
        self._entries[doc_id] = {
            "state": state,
            "content_hash": content_hash,
            "transitioned_at": transitioned_at,
        }
        self._entries.move_to_end(doc_id)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return self._entries.get(doc_id)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self._entries.values():
            out[entry["state"]] = out.get(entry["state"], 0) + 1
        return out

    def __len__(self) -> int:
        return len(self._entries)


def state_from_subject(subject: str) -> str | None:
    """``mcp.document.<state>.<tenant_id>`` → ``<state>`` (or None if unknown)."""
    parts = subject.split(".")
    if len(parts) >= 4 and parts[0] == "mcp" and parts[1] == "document":
        state = parts[2]
        if state in _VALID_STATES:
            return state
    return None


class NatsStatusSubscriber:
    """Consumes ``mcp.document.*.{tenant_id}`` into a :class:`StatusStore`."""

    def __init__(
        self,
        nc: Any,
        js: Any,
        tenant_id: str,
        store: StatusStore,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self._nc = nc
        self._js = js
        self.tenant_id = tenant_id
        self.store = store
        # on_event(doc_id, state) — seam for a future SSE / progress bridge.
        self._on_event = on_event

    def handle_message(self, subject: str, data: bytes) -> None:
        """Parse one status message into the store. Unit-testable without NATS."""
        import json  # noqa: PLC0415

        state = state_from_subject(subject)
        if state is None:
            logger.warning("status.unknown_subject subject=%s", subject)
            return
        try:
            payload = json.loads(data)
            doc_id = payload["doc_id"]
        except Exception:
            logger.warning("status.bad_message subject=%s", subject, exc_info=True)
            return
        self.store.record(
            doc_id,
            state,
            content_hash=payload.get("content_hash"),
            transitioned_at=payload.get("transitioned_at"),
        )
        if self._on_event is not None:
            self._on_event(doc_id, state)

    @classmethod
    async def connect(
        cls, *, url: str, tenant_id: str, store: StatusStore
    ) -> NatsStatusSubscriber:
        import nats  # noqa: PLC0415

        from .nats import warn_if_insecure_nats_url  # noqa: PLC0415

        warn_if_insecure_nats_url(url)
        nc = await nats.connect(url)
        js = nc.jetstream()
        return cls(nc, js, tenant_id, store)

    async def run(
        self,
        shutdown_event: anyio.Event,
        *,
        task_status: TaskStatus | None = None,
    ) -> None:
        """Durable pull-consumer loop. Requires a live broker (integration)."""
        import anyio  # noqa: PLC0415
        import nats.errors  # noqa: PLC0415

        subject = f"mcp.document.*.{self.tenant_id}"
        # Signal "task running" *before* the first (fallible) subscribe: bus
        # status is a non-critical observability path, so a broker that isn't
        # ready at startup should retry below rather than crash the lifespan.
        # ``started()`` therefore means "the subscriber loop is running", not
        # "the subscription succeeded".
        if task_status is not None:
            task_status.started()

        sub = None
        while not shutdown_event.is_set():
            if sub is None:
                try:
                    sub = await self._js.pull_subscribe(
                        subject, durable=f"mcp-status-{self.tenant_id}"
                    )
                except Exception:
                    # Broker not ready / transient connect error: back off and
                    # retry the subscribe instead of giving up.
                    logger.warning(
                        "NATS status subscribe failed; retrying", exc_info=True
                    )
                    await anyio.sleep(5)
                    continue
            try:
                msgs = await sub.fetch(batch=16, timeout=5)
            except nats.errors.TimeoutError:
                # Expected when idle: no messages within the fetch window. Loop
                # straight back to re-check shutdown — no log, no extra sleep.
                continue
            except Exception:
                # Real broker error (disconnect, auth failure, stream deleted):
                # drop the (possibly dead) subscription, back off, and
                # re-subscribe on the next iteration rather than hot-spinning.
                logger.warning(
                    "NATS status subscriber fetch failed; re-subscribing",
                    exc_info=True,
                )
                sub = None
                await anyio.sleep(5)
                continue
            for msg in msgs:
                self.handle_message(msg.subject, msg.data)
                await msg.ack()

    async def aclose(self) -> None:
        try:
            await self._nc.drain()
        except Exception:
            logger.warning("NATS status subscriber drain failed", exc_info=True)
