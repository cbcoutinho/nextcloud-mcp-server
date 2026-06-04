# ADR-028: Ingest transport port (local anyio vs distributed procrastinate)

## Status

Accepted — 2026-06-04

## Context

Document ingest (scanner/webhook → fetch → chunk → embed → upsert to Qdrant)
runs in one of two modes, selected by `INGEST_QUEUE`:

- `memory` (the SQLite/dev default): an in-process anyio
  `MemoryObjectStream` drained by a pool of in-process worker tasks.
- `postgres`: jobs are deferred into the per-tenant Postgres via
  [procrastinate](https://procrastinate.readthedocs.io/) and drained by a
  *separate* `nextcloud-mcp-server worker` process (the scale-to-zero
  api/worker split — KEDA scales the worker Deployment on queue depth).

ADR-007 introduced the in-process model; Deck #183 / PR #836 added the
postgres backend and a `TaskProducer` **Protocol** (`vector/queue/ports.py`)
so the scanner and webhook receiver send a `DocumentTask` to a backend-agnostic
sink — `MemoryTaskProducer` (anyio) or `ProcrastinateTaskProducer` (Postgres).

That abstracted the **producer** side, but left two gaps:

1. **The consumer side was not abstracted.** Memory mode spun up an in-process
   pool inside the server lifespan; postgres mode relied on the external worker
   CLI. Nothing tied these together.
2. **Mode selection leaked into the lifespan.** `app.py` carried a duplicated
   `if use_postgres: build producer + ensure_schema … else: create stream …`
   branch, plus a conditional N-worker startup and a
   `getattr(task_producer, "drain", None)` shutdown probe — repeated across the
   two near-identical lifespan paths (single-user BasicAuth and multi-user
   OAuth/BasicAuth). Adding a third backend (Redis/NATS/SQS) would have meant
   editing both blocks.

## Decision

Introduce an `IngestTransport` abstraction that owns **both** sides of one
ingest backend — the producer to wire into `app.state`/the scanner, and how (or
whether) the in-process consumer pool runs — built by a single
`build_transport(settings)` factory.

```
build_transport(settings) ->
    INGEST_QUEUE=postgres -> DistributedTransport(build_producer(settings))
    INGEST_QUEUE=memory   -> LocalTransport(vector_sync_queue_max_size)
```

`IngestTransport` (`vector/queue/transport.py`) exposes:

- `producer` — the `TaskProducer` to wire into `app.state` / hand to the scanner.
- `send_stream` / `receive_stream` — the raw anyio stream ends in memory mode,
  `None` for distributed backends (the latter keeps `ingest_status` queue-depth
  and the integration conftest's stream-singleton handling working unchanged).
- `run_consumers(task_group, spawn_worker, count)` — start the in-process pool;
  a **no-op** for distributed backends, whose consumer is the external worker.
- `aclose()` — tear down backend-owned resources once on shutdown.
  `DistributedTransport` closes the procrastinate connector pool;
  `LocalTransport` closes its owned send/receive stream ends (belt-and-suspenders
  — task-group cancellation already closes the per-worker clones, and anyio
  `aclose` is idempotent). The base default is a no-op.

The lifespan supplies a `spawn_worker` closure so the transport never learns
about auth modes — the single-user closure binds a shared `nc_client`+username,
the multi-user closure binds the Nextcloud host for per-document credential
resolution. Both forward anyio's injected `task_status` so `tg.start` observes
each worker's readiness.

### Why an ABC for the transport but a Protocol for the producer

`TaskProducer` is a `Protocol` specifically so anyio's third-party
`MemoryObjectSendStream` satisfies it structurally. The transport has exactly
two in-house adapters that share the `producer` storage and the
`receive_stream`/`run_consumers`/`aclose` defaults, so a concrete `abc.ABC` is
simpler, gives shared default implementations, and checks more cleanly under
`ty`. We keep `TaskProducer`/`build_producer` unchanged; the transport *wraps* a
producer.

### No consumer port

Deliberately, there is no consumer *port* (mirroring `ports.py`): in memory mode
the in-process pool is the consumer; in postgres mode the external worker is.
The worker CLI (`cli.py worker`) talks to procrastinate's `App` directly
(`run_worker_async`) — a different control surface from the in-process pool — so
it does not route through `IngestTransport`; `DistributedTransport.run_consumers`
is a no-op precisely because that separate process is the consumer.

### Single-tenant parallelism invariant

A single tenant must process its users' files **in parallel**, never one user
fully then the next. This holds by construction and is documented here as a
contract:

- **Local backend:** `LocalTransport.run_consumers` hands each of N workers
  (`VECTOR_SYNC_PROCESSOR_WORKERS`, default 3) an independent `clone()` of *one*
  shared receive stream. All users' `DocumentTask`s are multiplexed onto that
  single queue and dispatched **per-document**, so N documents — from any mix of
  users — are in flight at once.
- **Distributed backend:** the worker runs `run_worker_async(concurrency=N)`
  (default N = `VECTOR_SYNC_PROCESSOR_WORKERS`) over the single `ingest` queue,
  and procrastinate's only lock is a per-**document** `queueing_lock`
  (`user_id:doc_type:doc_id`) — there is no per-user lock — so different users'
  jobs run concurrently across worker slots and pods.

An explicit anyio overlap test for this invariant is tracked as a follow-up
(Deck #197).

## Consequences

- The two `app.py` lifespan paths are backend-agnostic: `build_transport()` +
  `_wire_vector_sync_state()` + `transport.run_consumers()` +
  `transport.aclose()`, with no `INGEST_QUEUE` branching and no `getattr` drain
  probe.
- Adding a future queue backend is one new `IngestTransport` adapter + one
  `build_transport` arm — no change to `app.py`, the scanner, or the webhook
  receiver.
- `app.state.task_producer` / `_vector_sync_state.task_producer` and the queue
  depth surface (`ingest_status.py`) keep their existing contracts.

## References

- ADR-007 — Background vector database synchronization (in-process anyio model)
- ADR-010 — Webhook-based vector database synchronization
- Deck #183 / PR #836 — procrastinate Postgres ingest queue + `TaskProducer` port
- Deck #196 — this work; Deck #197 — explicit parallelism test follow-up
