# Nextcloud MCP Server Helm Chart

This Helm chart deploys the Nextcloud MCP (Model Context Protocol) Server on a Kubernetes cluster, enabling AI assistants to interact with your Nextcloud instance.

## Prerequisites

- Kubernetes 1.19+
- Helm 3.0+
- A running Nextcloud instance (accessible from the Kubernetes cluster)
- Nextcloud credentials (username/password for basic auth OR OAuth client for OAuth mode)

## Installation

### Quick Start with Basic Authentication

```bash
# Add the Helm repository
helm repo add nextcloud-mcp https://cbcoutinho.github.io/nextcloud-mcp-server
helm repo update

# Install with basic auth (recommended for most users)
helm install nextcloud-mcp nextcloud-mcp/nextcloud-mcp-server \
  --set nextcloud.host=https://cloud.example.com \
  --set auth.basic.username=myuser \
  --set auth.basic.password=mypassword
```

### Using a values file

Create a `custom-values.yaml` file:

```yaml
nextcloud:
  host: https://cloud.example.com

auth:
  mode: basic
  basic:
    username: myuser
    password: mypassword

resources:
  limits:
    cpu: 1000m
    memory: 512Mi
  requests:
    cpu: 100m
    memory: 128Mi
```

Install with your custom values:

```bash
helm install nextcloud-mcp nextcloud-mcp/nextcloud-mcp-server -f custom-values.yaml
```

### OAuth Authentication Mode (Experimental)

**Warning:** OAuth mode is experimental and requires patches to the Nextcloud `user_oidc` app. See the [Authentication Guide](https://github.com/cbcoutinho/nextcloud-mcp-server#authentication) for details.

```yaml
nextcloud:
  host: https://cloud.example.com
  mcpServerUrl: https://mcp.example.com
  publicIssuerUrl: https://cloud.example.com

auth:
  mode: oauth
  oauth:
    # Optional: provide pre-registered client credentials
    # If not provided, will use Dynamic Client Registration
    clientId: "your-client-id"
    clientSecret: "your-client-secret"
    persistence:
      enabled: true
      size: 100Mi

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: mcp.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: nextcloud-mcp-tls
      hosts:
        - mcp.example.com
```

## Configuration

### Key Configuration Parameters

#### Nextcloud Connection

| Parameter | Description | Default |
|-----------|-------------|---------|
| `nextcloud.host` | URL of your Nextcloud instance (required) | `""` |
| `nextcloud.mcpServerUrl` | MCP server URL for OAuth callbacks (OAuth only, optional) | Smart default* |
| `nextcloud.publicIssuerUrl` | Public URL for browser-accessible OAuth authorization endpoint (OAuth only, optional) | Smart default** |

**Smart Defaults:**
- `*mcpServerUrl`: If not set, automatically uses ingress host (if enabled) or `http://localhost:8000` (for port-forward setups)
- `**publicIssuerUrl`: If not set, defaults to `nextcloud.host`. **Only used for authorization endpoints** that browsers must access. All server-to-server endpoints (token, JWKS, introspection, userinfo) use URLs from OIDC discovery without rewriting

#### Authentication

| Parameter | Description | Default |
|-----------|-------------|---------|
| `auth.mode` | Authentication mode: `basic` or `oauth` | `basic` |
| `auth.basic.username` | Nextcloud username (basic auth) | `""` |
| `auth.basic.password` | Nextcloud password (basic auth) | `""` |
| `auth.basic.existingSecret` | Use existing secret for credentials | `""` |
| `auth.oauth.clientId` | OAuth client ID (OAuth mode, optional) | `""` |
| `auth.oauth.clientSecret` | OAuth client secret (OAuth mode, optional) | `""` |
| `auth.oauth.persistence.enabled` | Enable persistent storage for OAuth | `true` |
| `auth.oauth.persistence.size` | Size of OAuth storage PVC | `100Mi` |

#### Data Storage

The `/app/data` directory is used for application data (token databases, Qdrant persistent storage, etc.). It is always mounted as writable to support the read-only root filesystem security context.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `dataStorage.enabled` | Enable persistent storage for `/app/data` | `false` |
| `dataStorage.size` | Size of data storage PVC | `1Gi` |
| `dataStorage.storageClass` | Storage class (leave empty for default) | `""` |
| `dataStorage.accessMode` | Access mode | `ReadWriteOnce` |
| `dataStorage.existingClaim` | Use existing PVC | `""` |

**When to enable persistence:**
- Multi-user basic auth with offline access (stores `tokens.db`)
- Qdrant persistent mode (stores vector database)
- Any feature requiring persistent app data

**When persistence is disabled:** Uses `emptyDir` (non-persistent, data lost on pod restart, but directory remains writable).

#### MCP Server Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `mcp.transport` | Transport mode | `streamable-http` |
| `mcp.port` | Server port (used by both auth modes) | `8000` |
| `mcp.extraArgs` | Additional command-line arguments | `[]` |

The `extraArgs` parameter allows you to pass additional command-line arguments to the MCP server. This is useful for enabling debug logging, enabling specific apps, or other runtime configuration.

**Example:**
```yaml
mcp:
  extraArgs:
    - "--log-level"
    - "debug"
    - "--enable-app"
    - "notes"
```

#### Image Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `image.repository` | Container image repository | `ghcr.io/cbcoutinho/nextcloud-mcp-server` |
| `image.pullPolicy` | Image pull policy | `IfNotPresent` |

**Note:** Image tag is automatically set to the chart's `appVersion` and cannot be overridden.

#### Resources

| Parameter | Description | Default |
|-----------|-------------|---------|
| `resources.limits.cpu` | CPU limit | `1000m` |
| `resources.limits.memory` | Memory limit | `512Mi` |
| `resources.requests.cpu` | CPU request | `100m` |
| `resources.requests.memory` | Memory request | `128Mi` |

#### Service

| Parameter | Description | Default |
|-----------|-------------|---------|
| `service.type` | Service type | `ClusterIP` |
| `service.port` | Service port | `8000` |

#### Ingress

| Parameter | Description | Default |
|-----------|-------------|---------|
| `ingress.enabled` | Enable ingress | `false` |
| `ingress.className` | Ingress class name | `""` |
| `ingress.hosts` | Ingress host configuration | See values.yaml |
| `ingress.tls` | Ingress TLS configuration | `[]` |

#### Autoscaling

| Parameter | Description | Default |
|-----------|-------------|---------|
| `autoscaling.enabled` | Enable HPA | `false` |
| `autoscaling.minReplicas` | Minimum replicas | `1` |
| `autoscaling.maxReplicas` | Maximum replicas | `10` |
| `autoscaling.targetCPUUtilizationPercentage` | Target CPU % | `80` |

#### Health Probes

| Parameter | Description | Default |
|-----------|-------------|---------|
| `livenessProbe.httpGet.path` | Liveness probe endpoint | `/health/live` |
| `livenessProbe.initialDelaySeconds` | Initial delay for liveness | `30` |
| `livenessProbe.periodSeconds` | Check interval for liveness | `10` |
| `readinessProbe.httpGet.path` | Readiness probe endpoint | `/health/ready` |
| `readinessProbe.initialDelaySeconds` | Initial delay for readiness | `10` |
| `readinessProbe.periodSeconds` | Check interval for readiness | `5` |

The application exposes HTTP health check endpoints:
- `/health/live` - Liveness probe (checks if application is running)
- `/health/ready` - Readiness probe (checks if application is ready to serve traffic)

#### Document Processing (Optional)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `documentProcessing.enabled` | Enable document processing | `false` |
| `documentProcessing.defaultProcessor` | Default processor | `unstructured` |
| `documentProcessing.unstructured.enabled` | Enable Unstructured.io processor | `false` |
| `documentProcessing.unstructured.apiUrl` | Unstructured API URL | `http://unstructured:8000` |
| `documentProcessing.tesseract.enabled` | Enable Tesseract OCR | `false` |

#### Vector Search & Semantic Capabilities (Optional)

Enable semantic search capabilities with BM25 hybrid search by deploying a vector database (Qdrant) and embedding service (Ollama or OpenAI).

**Semantic Search Configuration:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `semanticSearch.enabled` | Enable semantic search and background vector synchronization | `false` |
| `semanticSearch.scanInterval` | Scan interval in seconds | `3600` |
| `semanticSearch.processorWorkers` | Number of concurrent processor workers | `3` |
| `semanticSearch.queueMaxSize` | Maximum queue size for pending documents | `10000` |

**Document Chunking Configuration:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `documentChunking.chunkSize` | Number of words per chunk for embedding | `512` |
| `documentChunking.chunkOverlap` | Number of overlapping words between chunks | `50` |

**Chunking Strategy:**
- **Small chunks (256-384)**: Better precision for searches, more storage overhead
- **Medium chunks (512-768)**: Balanced approach (recommended for most use cases)
- **Large chunks (1024+)**: Better context preservation, less precise matching
- **Overlap**: Should be 10-20% of chunk size to preserve context across boundaries

**Qdrant Vector Database:**

Qdrant is deployed as a subchart when `qdrant.enabled` is `true`. All configuration values are passed through to the [qdrant/qdrant](https://github.com/qdrant/qdrant-helm) chart.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `qdrant.enabled` | Deploy Qdrant as a subchart | `false` |
| `qdrant.replicaCount` | Number of Qdrant replicas | `1` |
| `qdrant.image.tag` | Qdrant version | `v1.12.5` |
| `qdrant.apiKey` | Optional API key for authentication | `""` |
| `qdrant.persistence.size` | Storage size for vector data | `10Gi` |
| `qdrant.persistence.storageClass` | Storage class | `""` |
| `qdrant.resources.requests.cpu` | CPU request | `200m` |
| `qdrant.resources.requests.memory` | Memory request | `512Mi` |
| `qdrant.resources.limits.cpu` | CPU limit | `1000m` |
| `qdrant.resources.limits.memory` | Memory limit | `2Gi` |

**Ollama Embedding Service:**

Ollama is deployed as a subchart when `ollama.enabled` is `true`. All configuration values are passed through to the [ollama/ollama](https://github.com/otwld/ollama-helm) chart. Alternatively, set `ollama.url` to use an external Ollama instance.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `ollama.enabled` | Deploy Ollama as a subchart | `false` |
| `ollama.url` | External Ollama URL (use with `enabled: false`) | `""` |
| `ollama.embeddingModel` | Embedding model to use | `nomic-embed-text` |
| `ollama.verifySsl` | Verify SSL certificates | `true` |
| `ollama.replicaCount` | Number of Ollama replicas | `1` |
| `ollama.ollama.models.pull` | Models to pull on startup | `["nomic-embed-text"]` |
| `ollama.persistentVolume.enabled` | Enable persistent storage | `true` |
| `ollama.persistentVolume.size` | Storage size for models | `20Gi` |
| `ollama.resources.requests.cpu` | CPU request | `500m` |
| `ollama.resources.requests.memory` | Memory request | `1Gi` |
| `ollama.resources.limits.cpu` | CPU limit | `2000m` |
| `ollama.resources.limits.memory` | Memory limit | `4Gi` |

**OpenAI Embedding Provider (Alternative):**

Use OpenAI or any OpenAI-compatible API instead of Ollama.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `openai.enabled` | Enable OpenAI embedding provider | `false` |
| `openai.apiKey` | OpenAI API key | `""` |
| `openai.existingSecret` | Use existing secret for API key | `""` |
| `openai.secretKey` | Key in secret containing API key | `api-key` |
| `openai.baseUrl` | Custom API endpoint (optional) | `""` |

#### Observability & Monitoring

The chart includes comprehensive observability features including Prometheus metrics, OpenTelemetry tracing, and Grafana dashboards.

**Metrics Configuration:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `observability.metrics.enabled` | Enable Prometheus metrics | `true` |
| `observability.metrics.port` | Metrics port | `9090` |
| `observability.metrics.path` | Metrics endpoint path | `/metrics` |

**Tracing Configuration:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `observability.tracing.enabled` | Enable OpenTelemetry tracing | `false` |
| `observability.tracing.endpoint` | OTLP collector endpoint | `""` |
| `observability.tracing.serviceName` | Service name in traces | `nextcloud-mcp-server` |
| `observability.tracing.samplingRate` | Trace sampling rate (0.0-1.0) | `1.0` |

**Logging Configuration:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `observability.logging.format` | Log format (json or text) | `json` |
| `observability.logging.level` | Log level | `INFO` |
| `observability.logging.includeTraceContext` | Include trace IDs in logs | `true` |

**ServiceMonitor (Prometheus Operator):**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `serviceMonitor.enabled` | Create ServiceMonitor resource | `false` |
| `serviceMonitor.interval` | Scrape interval | `30s` |
| `serviceMonitor.scrapeTimeout` | Scrape timeout | `10s` |
| `serviceMonitor.labels` | Additional labels for ServiceMonitor | `{}` |

**PrometheusRule (Prometheus Operator):**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `prometheusRule.enabled` | Create PrometheusRule with alert rules | `false` |
| `prometheusRule.labels` | Additional labels for PrometheusRule | `{}` |

**Grafana Dashboards:**

| Parameter | Description | Default |
|-----------|-------------|---------|
| `dashboards.enabled` | Enable automatic dashboard provisioning | `false` |
| `dashboards.grafanaFolder` | Grafana folder name for dashboards | `Nextcloud MCP` |
| `dashboards.labels` | Additional labels for dashboard ConfigMap | `{}` |
| `dashboards.annotations` | Additional annotations for dashboard ConfigMap | `{}` |

When `dashboards.enabled` is `true`, a ConfigMap with the Grafana dashboard is created with the `grafana_dashboard: "1"` label. This enables automatic discovery by Grafana sidecar containers (commonly used with kube-prometheus-stack).

The dashboard provides comprehensive monitoring including:
- HTTP request metrics (RED pattern: Rate, Errors, Duration)
- MCP tool performance and errors
- Nextcloud API performance by app (notes, calendar, contacts, etc.)
- OAuth token operations and cache hit rates
- External dependency health (Nextcloud, Qdrant, Keycloak, Unstructured API)
- Vector sync processing pipeline (when enabled)

For manual import or more details, see `charts/nextcloud-mcp-server/dashboards/README.md`.

## Examples

### Example 1: Basic Auth with Ingress

```yaml
nextcloud:
  host: https://cloud.example.com

auth:
  mode: basic
  basic:
    username: admin
    password: secure-password

ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: mcp.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: mcp-tls
      hosts:
        - mcp.example.com

resources:
  limits:
    cpu: 2000m
    memory: 1Gi
  requests:
    cpu: 200m
    memory: 256Mi
```

### Example 2: Using Existing Secrets

#### Basic Auth with Existing Secret

Create a secret manually:

```bash
kubectl create secret generic nextcloud-credentials \
  --from-literal=username=myuser \
  --from-literal=password=mypassword
```

Then reference it in your values:

```yaml
nextcloud:
  host: https://cloud.example.com

auth:
  mode: basic
  basic:
    existingSecret: nextcloud-credentials
    usernameKey: username
    passwordKey: password
```

#### OAuth with Existing Secret (Pre-registered Client)

If you have a pre-registered OAuth client:

```bash
kubectl create secret generic nextcloud-oauth-creds \
  --from-literal=clientId=my-oauth-client-id \
  --from-literal=clientSecret=my-oauth-client-secret
```

Then reference it in your values:

```yaml
nextcloud:
  host: https://cloud.example.com
  # mcpServerUrl and publicIssuerUrl are optional!
  # If not set, mcpServerUrl defaults to ingress host or localhost
  # publicIssuerUrl defaults to nextcloud.host (only used for browser-accessible auth endpoint)

auth:
  mode: oauth
  oauth:
    existingSecret: nextcloud-oauth-creds
    clientIdKey: clientId
    clientSecretKey: clientSecret
    persistence:
      enabled: true

ingress:
  enabled: true
  hosts:
    - host: mcp.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: mcp-tls
      hosts:
        - mcp.example.com
```

### Example 3: OAuth with Document Processing and Dynamic Client Registration

This example shows OAuth without pre-registered credentials (using DCR) and optional URL values:

```yaml
nextcloud:
  host: https://cloud.example.com
  # mcpServerUrl will automatically use ingress host (https://mcp.example.com)
  # publicIssuerUrl will automatically default to nextcloud.host (only used for browser-accessible auth endpoint)

auth:
  mode: oauth
  oauth:
    # No clientId/clientSecret - will use Dynamic Client Registration!
    persistence:
      enabled: true
      storageClass: fast-ssd
      size: 200Mi

documentProcessing:
  enabled: true
  defaultProcessor: unstructured
  unstructured:
    enabled: true
    apiUrl: http://unstructured-api:8000
    strategy: hi_res
    languages: eng,deu,fra

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: mcp.example.com
      paths:
        - path: /
          pathType: Prefix
```

### Example 4: High Availability with Autoscaling

```yaml
replicaCount: 2

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 20
  targetCPUUtilizationPercentage: 70
  targetMemoryUtilizationPercentage: 80

resources:
  limits:
    cpu: 2000m
    memory: 1Gi
  requests:
    cpu: 500m
    memory: 512Mi

affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchExpressions:
              - key: app.kubernetes.io/name
                operator: In
                values:
                  - nextcloud-mcp-server
          topologyKey: kubernetes.io/hostname
```

### Example 5: Semantic Search with Qdrant and Ollama

Deploy with vector search capabilities using embedded Qdrant and Ollama:

```yaml
nextcloud:
  host: https://cloud.example.com

auth:
  mode: basic
  basic:
    username: admin
    password: secure-password

# Enable semantic search
semanticSearch:
  enabled: true
  scanInterval: 1800  # Scan every 30 minutes
  processorWorkers: 5

# Deploy Qdrant as a subchart
qdrant:
  enabled: true
  persistence:
    size: 20Gi
    storageClass: fast-ssd
  resources:
    requests:
      cpu: 500m
      memory: 1Gi
    limits:
      cpu: 2000m
      memory: 4Gi

# Deploy Ollama as a subchart
ollama:
  enabled: true
  embeddingModel: nomic-embed-text
  persistentVolume:
    size: 30Gi
    storageClass: standard
  resources:
    requests:
      cpu: 1000m
      memory: 2Gi
    limits:
      cpu: 4000m
      memory: 8Gi
```

Or use an external Ollama instance:

```yaml
semanticSearch:
  enabled: true

qdrant:
  enabled: true

# Use external Ollama instead of deploying subchart
ollama:
  enabled: false
  url: "http://ollama.ai-services.svc.cluster.local:11434"
  embeddingModel: nomic-embed-text
```

Or use OpenAI for embeddings:

```yaml
semanticSearch:
  enabled: true

qdrant:
  enabled: true

# Use OpenAI instead of Ollama
openai:
  enabled: true
  apiKey: "sk-..."
  # Or use existing secret:
  # existingSecret: openai-api-key
  # secretKey: api-key
```

## Upgrading

### To upgrade an existing deployment:

```bash
# Update the repository
helm repo update

# Upgrade with your custom values
helm upgrade nextcloud-mcp nextcloud-mcp/nextcloud-mcp-server -f custom-values.yaml
```

### To upgrade with new values:

```bash
helm upgrade nextcloud-mcp nextcloud-mcp/nextcloud-mcp-server \
  --set resources.limits.memory=1Gi
```

## Uninstalling

```bash
helm uninstall nextcloud-mcp
```

**Note:** This will delete all resources including PVCs. If you want to preserve OAuth client data, backup the PVC before uninstalling.

## Troubleshooting

### Check pod status

```bash
kubectl get pods -l app.kubernetes.io/name=nextcloud-mcp-server
```

### View logs

```bash
kubectl logs -l app.kubernetes.io/name=nextcloud-mcp-server --tail=100 -f
```

### Check health endpoints

The application exposes health check endpoints for monitoring:

```bash
# Port forward to the service
kubectl port-forward svc/nextcloud-mcp 8000:8000

# Check liveness (if app is running)
curl http://localhost:8000/health/live

# Check readiness (if app is ready to serve traffic)
curl http://localhost:8000/health/ready
```

**Example responses:**

Liveness (always returns 200 if running):
```json
{
  "status": "alive",
  "mode": "basic"
}
```

Readiness (returns 200 if ready, 503 if not ready):
```json
{
  "status": "ready",
  "checks": {
    "nextcloud_configured": "ok",
    "auth_mode": "basic",
    "auth_configured": "ok"
  }
}
```

### Common Issues

1. **Connection refused to Nextcloud**
   - Verify `nextcloud.host` is accessible from the Kubernetes cluster
   - For OAuth mode: Ensure MCP server can reach OIDC discovery endpoints (token, JWKS, introspection, userinfo URLs)
   - Check network policies and firewall rules
   - Note: Do not use internal Docker hostnames (like `http://app:80`) for `nextcloud.host` - use externally resolvable URLs

2. **Authentication failures**
   - For basic auth: verify username/password are correct
   - For OAuth: check that OIDC app is properly configured

3. **OAuth persistence issues**
   - Verify PVC is bound: `kubectl get pvc`
   - Check storage class exists: `kubectl get storageclass`

4. **Resource constraints**
   - Increase memory limits if seeing OOM errors
   - Adjust CPU requests based on load

## Security Considerations

1. **Secrets Management**: Consider using external secret management (e.g., Sealed Secrets, External Secrets Operator)
2. **TLS**: Always use TLS/HTTPS for production deployments
3. **Network Policies**: Restrict network access to necessary services only
4. **RBAC**: Review and customize ServiceAccount permissions as needed
5. **App Passwords**: For basic auth, use Nextcloud app passwords instead of main account passwords

## Support

- GitHub Issues: https://github.com/cbcoutinho/nextcloud-mcp-server/issues
- Documentation: https://github.com/cbcoutinho/nextcloud-mcp-server#readme

## License

This chart is licensed under AGPL-3.0, consistent with the Nextcloud MCP Server project.
