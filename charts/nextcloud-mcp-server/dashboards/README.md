# Grafana Dashboards

This directory contains example Grafana dashboards for monitoring the Nextcloud MCP Server.

## Dashboards

### nextcloud-mcp-server.json

Comprehensive dashboard with the following panels:

- **Request Rate**: HTTP requests per second by method and endpoint
- **Error Rate**: Percentage of 5xx errors
- **Request Latency**: P50 and P95 latency by endpoint
- **Top MCP Tools**: Most frequently called tools
- **Nextcloud API Latency**: API call latency by app (notes, calendar, etc.)
- **Vector Sync Queue**: Queue size for background document processing

## Importing to Grafana

### Manual Import

1. Open Grafana UI
2. Navigate to Dashboards → Import
3. Upload `nextcloud-mcp-server.json`
4. Select your Prometheus data source
5. Click "Import"

### Automated Import (Kubernetes)

If using the Grafana Operator or kube-prometheus-stack, you can create a ConfigMap:

```bash
kubectl create configmap nextcloud-mcp-dashboards \
  --from-file=nextcloud-mcp-server.json \
  -n monitoring

# Add label for Grafana sidecar to discover
kubectl label configmap nextcloud-mcp-dashboards \
  grafana_dashboard=1 \
  -n monitoring
```

Or add to your Helm values:

```yaml
# values.yaml for kube-prometheus-stack
grafana:
  dashboardProviders:
    dashboardproviders.yaml:
      apiVersion: 1
      providers:
        - name: 'nextcloud-mcp'
          orgId: 1
          folder: 'Nextcloud MCP'
          type: file
          disableDeletion: false
          editable: true
          options:
            path: /var/lib/grafana/dashboards/nextcloud-mcp

  dashboardsConfigMaps:
    nextcloud-mcp: nextcloud-mcp-dashboards
```

## Dashboard Variables

The dashboard includes two variables:

- **Data Source**: Select your Prometheus data source
- **Namespace**: Filter metrics by Kubernetes namespace

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
