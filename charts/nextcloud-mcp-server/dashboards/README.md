# Grafana Dashboards

This directory contains example Grafana dashboards for monitoring the Nextcloud MCP Server.

## Dashboards

### nextcloud-mcp-server.json

All-in-one Operations Dashboard with comprehensive monitoring across all system components.

#### Overview Row
High-level metrics for quick health assessment:
- **Request Rate** (stat): Total requests per second
- **Error Rate** (stat): Percentage of 5xx errors with color thresholds
- **P95 Latency** (stat): 95th percentile request latency
- **Active Requests** (stat): Current in-flight requests

#### HTTP Metrics (RED Pattern)
Core request/error/duration metrics:
- **Request Rate by Endpoint** (timeseries): RPS breakdown by endpoint
- **Error Rate by Status Code** (timeseries): Error rates for 4xx/5xx codes
- **Latency Percentiles** (timeseries): P50, P95, P99 latency trends
- **Status Code Distribution** (piechart): Percentage breakdown of all status codes

#### MCP Tools Row
MCP-specific tool performance:
- **Top Tools by Call Volume** (bargauge): Top 10 most-called tools
- **Tool Error Rate** (timeseries): Error rates per tool
- **Tool Execution Duration** (timeseries): P95 latency by tool

#### Nextcloud API Row
Backend API performance metrics:
- **API Calls by App** (timeseries): Request rate per Nextcloud app (notes, calendar, contacts, etc.)
- **API Latency by App** (timeseries): P95 latency per app
- **API Retries by Reason** (timeseries): Retry patterns (429, timeout, connection errors)
- **API Error Rate** (stat): Overall API error percentage

#### OAuth & Authentication Row
OAuth token operations and caching:
- **Token Validations** (timeseries): Success/failure rates for token validation
- **Token Exchange Operations** (timeseries): RFC 8693 token exchange operations
- **Token Cache Hit Rate** (stat): Percentage of cache hits (color-coded: red<50%, yellow<80%, green≥80%)
- **Refresh Token Operations** (timeseries): Refresh token storage operations by type

#### Dependencies & Health Row
External dependency status monitoring:
- **Nextcloud Health** (stat): UP/DOWN status with color coding
- **Qdrant Health** (stat): Vector database health status
- **Keycloak Health** (stat): Identity provider health status
- **Unstructured API Health** (stat): Document processing API status
- **Health Check Duration** (timeseries): Health check latency by dependency
- **Database Operation Latency** (timeseries): P95 latency for DB operations (SQLite, Qdrant)

#### Vector Sync Row (when enabled)
Document processing pipeline metrics:
- **Documents Processed Rate** (timeseries): Processing throughput by status (success/failure)
- **Processing Queue Depth** (gauge): Current queue size with thresholds (yellow>50, red>100)
- **Qdrant Operations** (timeseries): Vector database operations by type
- **Document Processing Duration** (timeseries): P95 processing latency

## Importing to Grafana

### Manual Import

1. Open Grafana UI
2. Navigate to Dashboards → Import
3. Upload `nextcloud-mcp-server.json`
4. Select your Prometheus data source
5. Click "Import"

### Automated Import (Helm Chart)

The Helm chart now supports automatic dashboard provisioning via Grafana sidecar pattern.

#### Option 1: Using Helm Chart (Recommended)

Enable dashboard provisioning in your Helm values:

```yaml
# values.yaml for nextcloud-mcp-server chart
dashboards:
  enabled: true
  grafanaFolder: "Nextcloud MCP"  # Folder name in Grafana
  labels: {}  # Additional labels if needed
```

Then deploy or upgrade:

```bash
helm upgrade --install nextcloud-mcp nextcloud-mcp-server \
  --set dashboards.enabled=true
```

The dashboard will be automatically imported by Grafana if the sidecar is configured
to watch for ConfigMaps with label `grafana_dashboard: "1"`.

#### Option 2: Using kube-prometheus-stack

If using kube-prometheus-stack with Grafana sidecar enabled, the dashboard will be
automatically discovered and imported. Ensure your Grafana deployment has:

```yaml
# kube-prometheus-stack values
grafana:
  sidecar:
    dashboards:
      enabled: true
      label: grafana_dashboard
      folder: /tmp/dashboards
      provider:
        foldersFromFilesStructure: true
```

#### Option 3: Manual ConfigMap Creation

For other Grafana setups, create a ConfigMap manually:

```bash
kubectl create configmap nextcloud-mcp-dashboard \
  --from-file=nextcloud-mcp-server.json \
  -n monitoring

# Add sidecar discovery label
kubectl label configmap nextcloud-mcp-dashboard \
  grafana_dashboard=1 \
  grafana_folder="Nextcloud MCP" \
  -n monitoring
```

## Dashboard Variables

The dashboard includes four template variables for dynamic filtering:

- **datasource**: Select your Prometheus data source
- **namespace**: Filter metrics by Kubernetes namespace (supports "All")
- **pod**: Filter by specific pod(s) - multi-select enabled (supports "All")
- **interval**: Query interval for rate calculations (1m, 5m, 10m, 30m, 1h - default: 5m)

## Customization

You can customize the dashboard by:

1. Adjusting refresh rate (default: 30s)
2. Modifying time range (default: last 6 hours)
3. Adding new panels for specific metrics
4. Adjusting thresholds in existing panels

## Metrics Reference

All metrics are documented in `/docs/observability.md`. Key metric prefixes:

- `mcp_http_*` - HTTP server metrics
- `mcp_tool_*` - MCP tool invocation metrics
- `mcp_nextcloud_api_*` - Nextcloud API call metrics
- `mcp_oauth_*` - OAuth token validation metrics
- `mcp_vector_sync_*` - Vector database sync metrics
- `mcp_db_*` - Database operation metrics
