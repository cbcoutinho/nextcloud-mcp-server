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

Metrics are automatically scraped if you have Prometheus Operator installed:

```bash
helm install nextcloud-mcp charts/nextcloud-mcp-server \
  --set observability.metrics.enabled=true \
  --set observability.tracing.enabled=true \
  --set observability.tracing.endpoint=http://opentelemetry-collector:4317 \
  --set serviceMonitor.enabled=true
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `METRICS_ENABLED` | `true` | Enable Prometheus metrics |
| `METRICS_PORT` | `9090` | Port for metrics endpoint |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | - | OTLP gRPC endpoint (e.g., `http://otel-collector:4317`). Tracing is enabled when this is set. |
| `OTEL_SERVICE_NAME` | `nextcloud-mcp-server` | Service name in traces |
| `OTEL_TRACES_SAMPLER` | `always_on` | Trace sampling strategy |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Sampling rate (0.0-1.0) |
| `LOG_FORMAT` | `json` | Log format (`json` or `text`) |
| `LOG_LEVEL` | `INFO` | Minimum log level |
| `LOG_INCLUDE_TRACE_CONTEXT` | `true` | Include trace IDs in logs |

### Helm Chart Configuration

```yaml
observability:
  metrics:
    enabled: true
    port: 9090
    path: /metrics

  tracing:
    enabled: true
    endpoint: "http://opentelemetry-collector:4317"
    samplingRate: 1.0

  logging:
    format: json
    level: INFO
    includeTraceContext: true

serviceMonitor:
  enabled: true
  interval: 30s
  scrapeTimeout: 10s
```

## Metrics

### HTTP Server Metrics (RED)

- `mcp_http_requests_total` - Total HTTP requests
- `mcp_http_request_duration_seconds` - Request latency histogram
- `mcp_http_requests_in_progress` - In-flight requests gauge

### MCP Tool Metrics

- `mcp_tool_calls_total` - Tool invocation count by status
- `mcp_tool_duration_seconds` - Tool execution latency
- `mcp_tool_errors_total` - Tool errors by type

### Nextcloud API Metrics

- `mcp_nextcloud_api_requests_total` - API calls by app and status
- `mcp_nextcloud_api_duration_seconds` - API latency by app
- `mcp_nextcloud_api_retries_total` - Retry count (429, timeout, etc.)

### OAuth Flow Metrics

- `mcp_oauth_token_validations_total` - Token validation count
- `mcp_oauth_token_exchange_total` - Token exchange operations
- `mcp_oauth_token_cache_hits_total` - Cache hit/miss rate
- `mcp_oauth_refresh_token_operations_total` - Refresh token storage ops

### Vector Sync Metrics (when enabled)

- `mcp_vector_sync_documents_scanned_total` - Documents discovered
- `mcp_vector_sync_documents_processed_total` - Processing results
- `mcp_vector_sync_processing_duration_seconds` - Processing latency
- `mcp_vector_sync_queue_size` - Current queue depth
- `mcp_qdrant_operations_total` - Qdrant DB operations

### Database Metrics

- `mcp_db_operations_total` - DB operations (SQLite, Qdrant)
- `mcp_db_operation_duration_seconds` - DB latency

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
sum(rate(mcp_http_requests_total[5m])) by (method, endpoint)
```

**Error Rate (%)**:
```promql
sum(rate(mcp_http_requests_total{status_code=~"5.."}[5m]))
  / sum(rate(mcp_http_requests_total[5m])) * 100
```

**P95 Latency**:
```promql
histogram_quantile(0.95,
  sum(rate(mcp_http_request_duration_seconds_bucket[5m])) by (le, endpoint)
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

See `charts/nextcloud-mcp-server/templates/prometheusrule.yaml` for complete definitions.

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
