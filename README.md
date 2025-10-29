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
# Install with basic auth (recommended for most users)
helm install nextcloud-mcp ./helm/nextcloud-mcp-server \
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
helm install nextcloud-mcp ./helm/nextcloud-mcp-server -f custom-values.yaml
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
| `nextcloud.publicIssuerUrl` | Public issuer URL for OAuth (OAuth only, optional) | Smart default** |

**Smart Defaults:**
- `*mcpServerUrl`: If not set, automatically uses ingress host (if enabled) or `http://localhost:8000` (for port-forward setups)
- `**publicIssuerUrl`: If not set, automatically defaults to `nextcloud.host` (which works when both clients and MCP server access Nextcloud at the same URL)

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

#### Image Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `image.repository` | Container image repository | `ghcr.io/cbcoutinho/nextcloud-mcp-server` |
| `image.tag` | Container image tag | `""` (uses chart appVersion) |
| `image.pullPolicy` | Image pull policy | `IfNotPresent` |

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
| `service.oauthPort` | OAuth service port | `8001` |

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
  # publicIssuerUrl defaults to nextcloud.host

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
  # publicIssuerUrl will automatically default to nextcloud.host

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

## Upgrading

### To upgrade an existing deployment:

```bash
helm upgrade nextcloud-mcp ./helm/nextcloud-mcp-server -f custom-values.yaml
```

### To upgrade with new values:

```bash
helm upgrade nextcloud-mcp ./helm/nextcloud-mcp-server \
  --set image.tag=0.21.0 \
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
   - Check network policies and firewall rules

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
