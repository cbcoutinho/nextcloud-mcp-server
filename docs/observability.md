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
> name**, whereas `mcp_tool_calls_total` uses the Python `func.__name__`. These
> differ for the OAuth tools (e.g. `tool_provision_access` vs
> `provision_nextcloud_access`). Don't join the two on `tool_name`.

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

- `mcp_oauth_token_validations_total` - Token validation count
- `mcp_oauth_token_cache_hits_total` - Cache hit/miss rate
- `mcp_oauth_refresh_token_operations_total` - Refresh token storage ops

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
  (`timeout` | `oom` | `error` | `oversize`)

Corpus shape, recorded before the oversize gate so the over-cap tail is visible:

- `astrolabe_document_ingest_size_bytes{doc_type}` - Source size distribution
- `astrolabe_document_ingest_rejected_total{doc_type,reason}` - Rejected pre-parse

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
