# Observability and Monitoring

The Nextcloud MCP Server includes comprehensive observability features for production deployments:

- **Prometheus metrics** for monitoring performance and health
- **OpenTelemetry distributed tracing** for debugging request flows
- **Structured JSON logging** with trace correlation
- **Kubernetes integration** via ServiceMonitor and PrometheusRule

## Quick Start

### Local Development with Prometheus

```bash
# Enable metrics (enabled by default)
export METRICS_ENABLED=true
export METRICS_PORT=9090

# Enable tracing (optional - tracing is enabled when OTEL_EXPORTER_OTLP_ENDPOINT is set)
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

# Start the server
docker-compose up -d mcp
```

Access metrics at: `http://localhost:9090/metrics`

### Kubernetes Deployment

For Kubernetes deployments with Helm, see the [Helm chart repository](https://github.com/cbcoutinho/helm-charts) which includes ServiceMonitor and PrometheusRule support.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `METRICS_ENABLED` | `true` | Enable Prometheus metrics |
| `METRICS_PORT` | `9090` | Port for metrics endpoint |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | - | OTLP gRPC endpoint (e.g., `http://otel-collector:4317`). Tracing is enabled when this is set. |
| `OTEL_SERVICE_NAME` | `nextcloud-mcp-server` | Service name in traces |
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` | Trace sampling strategy. Read directly from the environment by the OpenTelemetry SDK, not through our config — so it is env-var only (no `settings.toml` equivalent). |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Sampling rate (0.0-1.0), applied only by the `traceidratio` samplers. Also read directly by the SDK. |
| `LOG_FORMAT` | `json` | Log format (`json` or `text`) |
| `LOG_LEVEL` | `INFO` | Minimum log level |
| `LOG_INCLUDE_TRACE_CONTEXT` | `true` | Include trace IDs in logs |

### Helm Chart Configuration

The Helm chart has moved to a [separate repository](https://github.com/cbcoutinho/helm-charts). See its `values.yaml` for observability configuration options including metrics, tracing, logging, and ServiceMonitor settings.

## Metrics

### HTTP Server Metrics (RED)

- `mcp_http_requests_total` - Total HTTP requests
- `mcp_http_request_duration_seconds` - Request latency histogram
- `mcp_http_requests_in_progress` - In-flight requests gauge

### MCP Tool Metrics

- `mcp_tool_calls_total` - Tool invocation count by status
- `mcp_tool_duration_seconds` - Tool execution latency
- `mcp_tool_errors_total` - Tool errors by type
- `mcp_tool_outcomes_total` - How the SDK *delivered* each call: `success` |
  `tool_error` | `protocol_error`

> `mcp_tool_outcomes_total` labels `tool_name` with the tool's **registered MCP
> name**; the other three use the Python `func.__name__` of the decorated
> function. Every tool function is now named after the tool it registers, so the
> two agree — keep it that way when adding a tool, or the label silently splits.

### MCP Client Fleet Metrics

Records who is connecting and how tool results reach them. These exist as a
baseline for the mcp python-sdk 1.x → 2.x (protocol 2026-07-28) upgrade, whose
most consequential changes are silent — see the alerts below.

- `mcp_client_sessions_total{client_name, client_version, protocol_version}` -
  sessions observed, counted once per session. `client_version` is truncated to
  `major.minor`; `client_name` is length-clamped and collapses to `_other` past
  50 distinct identities (both halves of the cardinality bound, since
  `clientInfo` is caller-chosen).
- `mcp_client_capability{client_name, capability}` - 1/0 per `elicitation` /
  `sampling` / `roots`, from the client's most recent session.
- `mcp_elicitation_total{prompt, outcome, reason}` - elicitation results.
  `outcome` is `accepted` | `declined` | `cancelled` | `message_only`; `reason`
  splits the silent `message_only` fallback into `no_elicit_attr` |
  `not_implemented` | `error` (`none` otherwise).

### Nextcloud API Metrics

- `mcp_nextcloud_api_requests_total` - API calls by app and status
- `mcp_nextcloud_api_duration_seconds` - API latency by app
- `mcp_nextcloud_api_retries_total` - Retry count (429, timeout, etc.)

### OAuth Flow Metrics

- `mcp_oauth_token_validations_total{method, result, reason, client_id}` -
  Token validations. **This is the "why was a client disconnected" metric.** A
  rejection ends the MCP session and forces the user to re-authenticate, so
  `reason` and `client_id` are what turn a bare failure count into an
  actionable one.
  - `method`: `jwt` | `introspect` | `userinfo` | `allowlist` | `unknown`
  - `result`: `valid` | `invalid` (the caller's token) | `error` (ours).
    Derived from `reason`, not set independently, so the two cannot drift.
  - `reason`: `expired`, `inactive`, `bad_signature`, `bad_issuer`,
    `bad_audience`, `not_allowlisted`, `not_configured`, `network_error`,
    `unknown` — `none` when valid
  - `client_id`: read from the *unverified* token, so length-clamped and capped
    at 50 distinct values (`_other` beyond), on a budget of its own

  > On a deployment with **no** validator configured, expect
  > `method="introspect"`, not `"jwt"`. Both entry points gate on
  > `self.jwks_client` before attempting JWT verification, so an unconfigured
  > JWKS means tokens fall through to introspection rather than being rejected
  > as `method="jwt"`. Alert on `reason="not_configured"` rather than on a
  > particular `method`.
- `mcp_oauth_grants_total{grant_type, result, refresh_token}` - Grants
  processed by the AS proxy token endpoint. **This is the "why is a client
  re-logging-in" metric**, and it complements the validation metric above: a
  rejection disconnects a client, but so does never having been given a
  refresh token in the first place.
  - `grant_type`: `authorization_code` | `refresh_token` | `unsupported`
  - `result`: `success` | `error` — an exhaustive partition of *attempted
    grants*: success is recorded by the grant handlers (only they can see
    whether a refresh token came back), while both failure modes are counted
    centrally in `oauth_token_endpoint` — a returned non-200 (fifteen such
    exits across the two handlers) and a raised exception, which is what an
    IdP timeout or a malformed token body actually produces. Counting only
    returned responses would let an IdP outage vanish from the metric
    entirely, which is the failure mode most worth seeing.
  - `refresh_token`: `issued` | `absent` | `unknown` (the grant failed, so
    there was no response to inspect)

  > A healthy client shows **one** `authorization_code` and then a steady
  > trickle of `refresh_token` grants. `authorization_code` repeating every
  > hour with zero `refresh_token` grants means the client never received a
  > refresh token and is re-running the full consent flow on every access-token
  > expiry — a user-visible re-login each time.
  >
  > `refresh_token="absent"` on a **`refresh_token` grant** is not a fault: an
  > IdP that does not rotate refresh tokens returns only a new access token.
  > It is `absent` on the **`authorization_code`** grant that is the bug.
  >
  > When a refresh token is missing, check which OIDC client the IdP actually
  > applied. The AS proxy exchanges the code as *this server's own confidential
  > client* (`MCP_SERVER_CLIENT_ID`), not the client-facing one, so
  > `offline_access` has to be enabled on that client — enabling it only on the
  > MCP client's registration has no effect on this exchange.
- `mcp_oauth_token_cache_hits_total` - Cache hit/miss rate
- `mcp_oauth_refresh_token_operations_total` - Refresh token storage ops

#### Reading the token-endpoint logs

Every hop of a grant is logged with credentials fingerprinted rather than
printed: secret fields become `<len=N sha256=xxxxxxxx>`. The fingerprint is a
truncated, unsalted SHA-256, so the *same* token can be followed across hops
and across process restarts without the credential ever being written down.
Non-secret fields (`scope`, `expires_in`, `token_type`, `error`) pass through
untouched, because those are what actually answer the question.

Did the IdP issue a refresh token?

```logql
{namespace="<tenant>", container="nextcloud-mcp-server"}
  |= "AS proxy: IdP token response" |= "refresh_token=ABSENT"
```

How often is a client re-running the full flow? (`client_id` is inside
`params=`, so filter on it to scope to one client.)

```logql
sum(count_over_time(
  {namespace="<tenant>", container="nextcloud-mcp-server"}
    |= "AS proxy token request" |= "grant_type=authorization_code" [1h]))
```

Deliberately parser-free. These logs are emitted as JSON, so `| logfmt` does
not apply, and the `params=` tail is a Python `dict` repr rather than
`key="value"` pairs — a parser stage here buys nothing and fails quietly. For
the aggregate breakdown by grant type, use `mcp_oauth_grants_total` above
rather than parsing logs at all; the log line is for per-client attribution,
which the metric deliberately does not carry.

Follow one refresh token across every hop it appears on — same `sha256=`
on each:

```logql
{namespace="<tenant>", container="nextcloud-mcp-server"}
  |= "AS proxy" |= "sha256=<fingerprint>"
```

The fingerprint is the selective filter here, so don't narrow further by
message text. One token shows up on **four** lines — the IdP's response, what
was handed to the client, the client's next refresh request, and the refreshed
response — and every one of them begins `AS proxy`. Enumerating a couple of
those prefixes in a regex is how you end up following half a token's life and
concluding it was never reused.

### Vector Sync Metrics (when enabled)

- `mcp_vector_sync_documents_scanned_total` - Documents discovered
- `mcp_vector_sync_documents_processed_total` - Processing results
- `mcp_vector_sync_processing_duration_seconds` - Processing latency
- `mcp_vector_sync_queue_size` - Current queue depth
- `mcp_vector_sync_dead_lettered_documents` - Documents parked as permanently-failed
- `mcp_qdrant_operations_total` - Qdrant DB operations

**Alerting on permanently-failed documents.** Prefer the
`mcp_vector_sync_dead_lettered_documents` **gauge** over the
`astrolabe_document_dead_lettered_total` / `astrolabe_document_parse_failed_total`
counters. Those counters only ever increment in the ingest worker, whose container
is not a scrape target, and whose tiers are scaled to zero between batches — so a
counter can fire and terminate without ever being scraped, and resets on scale-up.
The gauge is published by the long-lived (scraped) backend from the dead-letter
tombstones in Qdrant, so it survives worker lifecycle:

```promql
# Any document given up on — parse failures never retried until its etag changes.
mcp_vector_sync_dead_lettered_documents > 0
```

A dead-lettered document looks fully indexed to every count-based check (the
tombstone keeps folder totals reconciling) and its ingest job completes with
status *success*, so this gauge is the only signal that it is missing from search.

### Document Ingest Metrics

Throughput at the parse boundary, labelled by `processor` and `tier`
(`fast` | `structured` | `ocr`). Throughput counters accrue only on a full
success, so partial extractions and in-flight batch-OCR polls never inflate them.

- `astrolabe_document_parse_total{status}` - Parse attempts (doc rate)
- `astrolabe_document_pages_processed_total` - Pages extracted (page rate)
- `astrolabe_document_chars_processed_total` - Characters extracted
- `astrolabe_document_bytes_processed_total` - Source bytes parsed
- `astrolabe_document_parse_duration_seconds` - Parse latency per document
- `astrolabe_document_parse_failed_total{reason}` - Hard failures
  (`timeout` | `oom` | `error` | `oversize` | `unreadable` | `unsupported_type`).
  `unsupported_type` means no registered processor claims the document's mime
  type — a property of what this deployment has enabled, not of the document, so
  it is answered by enabling a processor (or excluding the type), not by a retry.

Corpus shape, recorded before the oversize gate so the over-cap tail is visible:

- `astrolabe_document_ingest_size_bytes{doc_type}` - Source size distribution
- `astrolabe_document_ingest_rejected_total{doc_type,reason}` - Rejected pre-parse

### Usage metering (billing)

**These are not Prometheus series.** They are rows in the app-DB `usage_events`
table, written only when `USAGE_METERING_ENABLED=true` and pulled by the control
plane's rollup — so don't go looking for them in Grafana. Writes are best-effort:
a metering failure is logged and never breaks ingest.

Two kinds, and the difference decides how the control plane aggregates them:

**Ingest flows** — emitted per document, per indexing pass
(`record_indexing_usage`, `vector/processor.py`). Re-indexing a document emits
again, so a period sum measures *churn*:

- `tokens_embedded` - Embedding request tokens. Hybrid documents only —
  keyword-index documents embed nothing and skip the row entirely.
- `pages_embedded` - Pages parsed. Text doc types (notes, Deck cards, news,
  mail) are never parsed and carry no page count, so they accrue no row.
- `pages_ocr` - Pages that hit the OCR tier specifically, billed separately
  from CPU-cheap parsing. Mode-independent.
- `bytes_ingested` / `bytes_stored` - Source size and persisted chunk-text size.

**Retention stock** — emitted once per UTC day (`record_storage_stock`,
`vector/metrics_publisher.py`):

- `chunks_stored` - Chunks currently retained, split by `index_mode`
  (`hybrid` | `keyword`) in the event metadata.

A month of `chunks_stored` readings sums to **chunk-days** — the integral of a
changing corpus — so a corpus loaded or purged mid-month is charged pro rata.
The event id is derived from `(metric, mode, UTC date)` and the insert is
`ON CONFLICT DO NOTHING`, so pod restarts cannot inflate the total; shortening
`USAGE_STOCK_SNAPSHOT_INTERVAL` does not bill more.

> Adding a metric here is a cross-repo change: the control-plane rollup silently
> ignores a metric its catalog does not know, so it bills nothing until the CP
> catalog and Stripe meter learn it too.

### Database Metrics

- `mcp_db_operations_total` - DB operations (SQLite, Postgres, Qdrant)
- `mcp_db_operation_duration_seconds` - DB latency, **including connection
  acquisition**
- `mcp_db_connect_duration_seconds` - Connection-acquisition latency alone

Postgres uses `NullPool` (ADR-026), so a connection is opened per operation and
`mcp_db_connect_duration_seconds` fires once per op — its `_count` rate tracking
the operation rate is expected, not a leak. Compare the two histograms to split
"the query is slow" from "getting a connection is slow":

```promql
# What fraction of an operation is spent just getting a connection?
sum(rate(mcp_db_connect_duration_seconds_sum{db="postgresql"}[5m]))
  / sum(rate(mcp_db_operation_duration_seconds_sum{db="postgresql"}[5m]))
```

A high ratio means the cost is the handshake (network path, TLS, pooler
placement), not the SQL. Deck #678 was exactly this: a PgBouncer replica
scheduled in another region made ~50% of connects pay a transatlantic TLS
handshake (~600ms vs ~30ms), which presented as a "slow `usage_events` insert"
because one span covered both halves.

### Dependency Health

- `mcp_dependency_health` - External dependency status (1=up, 0=down)
- `mcp_dependency_check_duration_seconds` - Health check latency

## Distributed Tracing

### Span Hierarchy

```
HTTP POST /messages
├── mcp.tool.nc_notes_create_note
│   └── nextcloud.api.notes.POST
│       └── httpx request (auto-instrumented)
└── oauth.token.validate (if OAuth mode)
    └── httpx request to IdP
```

### Span Attributes

- **MCP tools**: `mcp.tool.name`, `mcp.tool.args` (sanitized)
- **Nextcloud API**: `nextcloud.app`, `http.method`, `http.status_code`
- **OAuth**: `oauth.operation`, `oauth.method`
- **Vector sync**: `vector_sync.operation`, `vector_sync.document_count`

### Trace Context in Logs

When tracing is enabled, all logs include `trace_id` and `span_id`:

```json
{
  "timestamp": "2025-01-09T12:34:56.789Z",
  "level": "INFO",
  "logger": "nextcloud_mcp_server.server.notes",
  "message": "Note created successfully",
  "trace_id": "a1b2c3d4e5f6...",
  "span_id": "123456789abc...",
  "note_id": 42
}
```

### Tool-Call Logs

Every tool call emits one line, from the single `CallToolRequest` handler
(`instrument_call_tool_outcomes`) rather than per tool, so coverage is automatic
for every tool and every transport. Spans carry the same name and arguments, but
traces are sampled and short-retention — these lines are what answer "which
tools ran, with what inputs" over a month.

```json
{
  "message": "tool call nc_semantic_search success in 3428ms",
  "logger": "nextcloud_mcp_server.observability.metrics",
  "mcp_tool": "nc_semantic_search",
  "mcp_tool_args": "{'query': 'safeguarding policy review', 'limit': 3}",
  "duration_ms": 3428,
  "outcome": "success",
  "mcp_user": "alice",
  "mcp_client_id": "claude-ai"
}
```

| Field | Notes |
|-------|-------|
| `mcp_tool` | Registered tool name, or `unknown` if the caller sent a name that is not registered (bounds cardinality for anything aggregating this field) |
| `mcp_tool_requested` | The raw requested name — present **only** when it differs from `mcp_tool` |
| `mcp_tool_args` | Arguments as sent on the wire: defaults are absent, `ctx`/`etag` dropped, values whose key looks secret (`password`, `*token*`, `*secret*`, `api_key`, `credential`, `authorization`, `cookie` — substring, case-insensitive) replaced with `[redacted]`, each value capped at 200 chars and the whole field at 1000. Absent when there are no arguments |
| `duration_ms` | Wall-clock, including the SDK's validation and serialization |
| `outcome` | `success` \| `tool_error` (`isError=True`, the model sees it) \| `protocol_error` (JSON-RPC error, it does not). Same strings as `mcp_tool_outcomes_total` |
| `mcp_user`, `mcp_client_id` | From the verified access token; absent in BasicAuth / single-user / stdio, where there is no OAuth identity |

Successful calls log at INFO, the two error outcomes at WARNING. Requires
`LOG_FORMAT=json` for the fields to be queryable:

```logql
sum by (mcp_tool) (count_over_time(
  {namespace="tenant-example"} | json | mcp_tool != "" [1h]))
```

## Dashboards

### Prometheus Queries

**Request Rate (req/s)**:
```promql
sum(rate(mcp_http_requests_total[5m])) by (method, exported_endpoint)
```

> Group by `exported_endpoint`, **not** `endpoint`. The application sets
> `endpoint`, but the Prometheus Operator's ServiceMonitor injects a target
> label of the same name whose value is the Service port name (`metrics`).
> With the default `honorLabels: false`, Prometheus keeps the target label and
> renames the scraped one to `exported_endpoint`. Grouping by `endpoint`
> silently collapses every route into a single series.

**Error Rate (%)**:
```promql
sum(rate(mcp_http_requests_total{status_code=~"5.."}[5m]))
  / sum(rate(mcp_http_requests_total[5m])) * 100
```

**P95 Latency**:
```promql
histogram_quantile(0.95,
  sum(rate(mcp_http_request_duration_seconds_bucket[5m])) by (le, exported_endpoint)
)
```

**Top Tools by Volume**:
```promql
topk(10, sum(rate(mcp_tool_calls_total[5m])) by (tool_name))
```

**Nextcloud API Health**:
```promql
sum(rate(mcp_nextcloud_api_requests_total{status_code!~"2.."}[5m])) by (app)
```

**Why is a client being disconnected?** — the first query to run when a
connector reports dropping out. A rejection forces the user to log in again, so
even a handful a day is user-visible:
```promql
sum by (client_id, method, reason) (
  increase(mcp_oauth_token_validations_total{result!="valid"}[1h])
) > 0
```

Split by who is at fault: `result="invalid"` is the caller's token (expired,
revoked, wrong audience); `result="error"` is ours (`not_configured`,
`network_error`) and should page:
```promql
sum by (reason) (increase(mcp_oauth_token_validations_total{result="error"}[15m])) > 0
```

`reason="not_configured"` deserves its own alert — a missing JWKS or
introspection endpoint, or an unset `ALLOWED_MGMT_CLIENT`, rejects *every*
token, so it breaks all clients at once. Note the deliberate split on the
allowlist path: an empty allowlist is `not_configured` (ours, pageable) while a
client merely absent from a populated one is `not_allowlisted` (theirs):
```promql
sum(increase(mcp_oauth_token_validations_total{reason="not_configured"}[5m])) > 0
```

**MCP client fleet — who is connected, at which protocol version**:
```promql
sum by (client_name, client_version, protocol_version) (
  increase(mcp_client_sessions_total[1d])
)
```

**Protocol-version change (the SDK-upgrade tripwire)** — fires when any client
reports more than one protocol version in a day, i.e. a client started
negotiating something new:
```promql
count by (client_name) (
  count by (client_name, protocol_version) (increase(mcp_client_sessions_total[1d]) > 0)
) > 1
```

### When a fleet metric is empty

`mcp_client_sessions_total` and `mcp_client_capability` populate from the
`tools/call` handler. If they have **no series at all** while
`mcp_tool_outcomes_total` does, the recording is bailing out early and the
server says which branch, once per process, at WARNING:

| Log line contains | Meaning |
|---|---|
| `no request context on a tool call` | the instrumentation is not wired into the handler it expects |
| `has no client_params on a tool call` | the session handling the call is not the one that ran `initialize` |

```logql
{namespace=~"tenant-.*", container="nextcloud-mcp-server"}
  |= "MCP client fleet metrics"
```

Logged once per cause per process deliberately — this runs on the tool-call hot
path, so a per-request line would be its own incident. A restart re-arms it.

**Client-identity cardinality cap hit** — `client_name` collapses to `_other`
past 50 distinct identities. The real fleet is single digits, so this firing
means either a genuinely new class of client or a peer rotating its declared
`clientInfo.name` per session:
```promql
sum(increase(mcp_client_sessions_total{client_name="_other"}[1h])) > 0
```

**Client identity stopped resolving** — the accessors that read `clientInfo` /
`capabilities` / `protocolVersion` off the SDK session degrade to `"unknown"`
rather than raising, so an SDK field rename shows up here rather than in the
logs. Non-zero means the instrumentation needs updating for the new SDK shape:
```promql
sum(increase(mcp_client_sessions_total{client_name="unknown"}[1h])) > 0
```

**Tool errors arriving as protocol errors** — exactly 0 on mcp 1.x by
construction, since the SDK converts every exception except
`UrlElicitationRequiredError` into `CallToolResult(isError=True)`. Any non-zero
reading means tool failures stopped being visible to the model:
```promql
sum(increase(mcp_tool_outcomes_total{outcome="protocol_error"}[1h])) > 0
```

**Elicitation lost its back-channel** — `not_implemented` is the normal case for
clients that never supported elicitation, so it is excluded; the other reasons
appearing is the regression:
```promql
sum by (prompt, reason) (
  increase(mcp_elicitation_total{outcome="message_only", reason!="not_implemented"}[1h])
) > 0
```

**Ingest throughput — documents/s and pages/s**:

Both are counters, so the per-second rate comes from `rate()` rather than a
gauge. Pages/s is the more stable capacity signal, because documents vary from
one page to several thousand.

```promql
# documents/s by tier
sum(rate(astrolabe_document_parse_total{status="success"}[5m])) by (tier)

# pages/s by tier
sum(rate(astrolabe_document_pages_processed_total[5m])) by (tier)

# mean pages per document (how page-heavy this tenant's corpus is)
sum(rate(astrolabe_document_pages_processed_total[5m]))
  / sum(rate(astrolabe_document_parse_total{status="success"}[5m]))

# seconds per page — compare tiers, or spot a slow corpus
sum(rate(astrolabe_document_parse_duration_seconds_sum{status="success"}[5m]))
  / sum(rate(astrolabe_document_pages_processed_total[5m]))
```

**Corpus size distribution and how much the cap turns away**:
```promql
# p50 / p99 source size
histogram_quantile(0.99,
  sum(rate(astrolabe_document_ingest_size_bytes_bucket[1h])) by (le)
)

# fraction of documents rejected as oversize
sum(rate(astrolabe_document_ingest_rejected_total{reason="oversize"}[1h]))
  / sum(rate(astrolabe_document_ingest_size_bytes_count[1h]))
```

## Alerts

### Recommended Alert Rules

**Critical**:
- Server down for >5min
- Error rate >5% for >5min
- P95 latency >1s for >5min
- Dependency down for >2min

**Warning**:
- Token validation errors >1% for >10min
- Vector sync queue >100 for >15min
- Qdrant slow (p95 >500ms) for >10min
- Clients re-authorizing instead of refreshing (below)

A client that is never issued a refresh token silently degrades into a
re-login on every access-token expiry. Nothing fails, so nothing else alerts —
it surfaces only as users complaining that the connector keeps disconnecting:

```promql
# authorization_code grants that yielded no refresh token
sum(increase(mcp_oauth_grants_total{
      grant_type="authorization_code", refresh_token="absent"}[1h])) > 0
```

```promql
# repeated full authorization flows with no refresh grants to match:
# more than 2 re-authorizations an hour is a client stuck in a re-login loop.
# result="success" on purpose — this counts completed re-logins, so a client
# retrying a stale code cannot inflate it into a false page.
sum(increase(mcp_oauth_grants_total{
      grant_type="authorization_code", result="success"}[1h])) > 2
  and
sum(increase(mcp_oauth_grants_total{
      grant_type="refresh_token", result="success"}[1h])) == 0
```

```promql
# the complementary signal, now that failures are counted too: a client
# failing grants outright (replayed codes, PKCE mismatch, expired proxy code)
sum by (grant_type) (increase(mcp_oauth_grants_total{result="error"}[15m])) > 0
```

See the [Helm chart repository](https://github.com/cbcoutinho/helm-charts) for PrometheusRule definitions.

## Troubleshooting

### Metrics Not Appearing

1. Check metrics are enabled: `curl http://localhost:9090/metrics`
2. Verify ServiceMonitor labels match Prometheus selector
3. Check Prometheus target status: `http://prometheus:9090/targets`

### Traces Not Appearing

1. Verify OTLP endpoint is reachable: `curl http://otel-collector:4317`
2. Check collector logs for errors
3. Verify sampling rate is not 0.0
4. Check trace backend (Jaeger/Tempo) connectivity

### High Cardinality Metrics

If you see cardinality warnings:
- Middleware normalizes endpoints (e.g., `/user/123` → `/user/*`)
- OAuth tokens are never included in metric labels
- User IDs are not tracked (use tracing for per-user debugging)

## Performance Impact

- **Metrics**: <1% overhead (counters/histograms are very fast)
- **Tracing**: ~2-5% overhead at 100% sampling
- **JSON logging**: <1% overhead vs text logging

**Recommendation**: Always enable metrics. Enable tracing in staging/production with 10-50% sampling.

## Architecture

The observability stack integrates at multiple layers:

1. **HTTP Layer**: `ObservabilityMiddleware` tracks all HTTP requests
2. **MCP Layer**: Tools use `@instrument_tool` for automatic metrics and trace span creation
3. **Client Layer**: `BaseNextcloudClient` tracks all API calls
4. **OAuth Layer**: Token operations are traced and metered
5. **Background Tasks**: Vector sync operations emit metrics/traces

All components use shared Prometheus `Registry` and OpenTelemetry `TracerProvider`.

## References

- [Prometheus Best Practices](https://prometheus.io/docs/practices/)
- [OpenTelemetry Python SDK](https://opentelemetry.io/docs/languages/python/)
- [Prometheus Operator](https://prometheus-operator.dev/)
- [Grafana Dashboards](https://grafana.com/docs/grafana/latest/dashboards/)
