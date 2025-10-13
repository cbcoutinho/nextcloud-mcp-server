# Configuration

The Nextcloud MCP server requires configuration to connect to your Nextcloud instance. Configuration is provided through environment variables, typically stored in a `.env` file.

## Quick Start

Create a `.env` file based on `env.sample`:

```bash
cp env.sample .env
# Edit .env with your Nextcloud details
```

Then choose your authentication mode:

- [OAuth2/OIDC Configuration](#oauth2oidc-configuration) (Recommended)
- [Basic Authentication Configuration](#basic-authentication-legacy)

---

## OAuth2/OIDC Configuration

OAuth2/OIDC is the recommended authentication mode for production deployments.

### Minimal Configuration (Auto-registration)

```dotenv
# .env file for OAuth with auto-registration
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# Leave these EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

This minimal configuration uses dynamic client registration to automatically register an OAuth client at startup.

### Full Configuration (Pre-configured Client)

```dotenv
# .env file for OAuth with pre-configured client
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# OAuth Client Credentials (optional - auto-registers if not provided)
NEXTCLOUD_OIDC_CLIENT_ID=your-client-id
NEXTCLOUD_OIDC_CLIENT_SECRET=your-client-secret

# OAuth Storage and Callback Settings (optional)
NEXTCLOUD_OIDC_CLIENT_STORAGE=.nextcloud_oauth_client.json
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000

# Leave these EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEXTCLOUD_HOST` | ✅ Yes | - | Full URL of your Nextcloud instance (e.g., `https://cloud.example.com`) |
| `NEXTCLOUD_OIDC_CLIENT_ID` | ⚠️ Optional | - | OAuth client ID (auto-registers if empty) |
| `NEXTCLOUD_OIDC_CLIENT_SECRET` | ⚠️ Optional | - | OAuth client secret (auto-registers if empty) |
| `NEXTCLOUD_OIDC_CLIENT_STORAGE` | ⚠️ Optional | `.nextcloud_oauth_client.json` | Path to store auto-registered client credentials |
| `NEXTCLOUD_MCP_SERVER_URL` | ⚠️ Optional | `http://localhost:8000` | MCP server URL for OAuth callbacks |
| `NEXTCLOUD_USERNAME` | ❌ Must be empty | - | Leave empty to enable OAuth mode |
| `NEXTCLOUD_PASSWORD` | ❌ Must be empty | - | Leave empty to enable OAuth mode |

### Prerequisites

Before using OAuth configuration:

1. **Install Nextcloud OIDC app** - Navigate to Apps → Security in your Nextcloud instance
2. **Enable dynamic client registration** (if using auto-registration) - Settings → OIDC
3. **Apply Bearer token patch** (if using non-OCS endpoints) - See [oauth2-bearer-token-session-issue.md](oauth2-bearer-token-session-issue.md)

See the [OAuth Setup Guide](oauth-setup.md) for detailed instructions.

---

## Basic Authentication (Legacy)

Basic Authentication is maintained for backward compatibility. It uses username and password credentials.

> [!WARNING]
> **Security Notice:** Basic Authentication stores credentials in environment variables and is less secure than OAuth. Use OAuth for production deployments.

### Configuration

```dotenv
# .env file for BasicAuth mode
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
NEXTCLOUD_USERNAME=your_nextcloud_username
NEXTCLOUD_PASSWORD=your_app_password_or_password
```

### Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXTCLOUD_HOST` | ✅ Yes | Full URL of your Nextcloud instance |
| `NEXTCLOUD_USERNAME` | ✅ Yes | Your Nextcloud username |
| `NEXTCLOUD_PASSWORD` | ✅ Yes | **Recommended:** Use a dedicated [Nextcloud App Password](https://docs.nextcloud.com/server/latest/user_manual/en/session_management.html#managing-devices). Generate one in Nextcloud Security settings. Alternatively, use your login password (less secure). |

---

## Loading Environment Variables

After creating your `.env` file, load the environment variables:

### On Linux/macOS

```bash
# Load all variables from .env
export $(grep -v '^#' .env | xargs)
```

### On Windows (PowerShell)

```powershell
# Load variables from .env
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)\s*=\s*(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), "Process")
    }
}
```

### Via Docker

```bash
# Docker automatically loads .env when using --env-file
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

---

## CLI Configuration

Some configuration options can also be provided via CLI arguments. CLI arguments take precedence over environment variables.

### OAuth-related CLI Options

```bash
uv run nextcloud-mcp-server --help

Options:
  --oauth / --no-oauth            Force OAuth mode (if enabled) or
                                  BasicAuth mode (if disabled). By default,
                                  auto-detected based on environment
                                  variables.
  --oauth-client-id TEXT          OAuth client ID (can also use
                                  NEXTCLOUD_OIDC_CLIENT_ID env var)
  --oauth-client-secret TEXT      OAuth client secret (can also use
                                  NEXTCLOUD_OIDC_CLIENT_SECRET env var)
  --oauth-storage-path TEXT       Path to store OAuth client credentials
                                  (can also use
                                  NEXTCLOUD_OIDC_CLIENT_STORAGE env var)
                                  [default: .nextcloud_oauth_client.json]
  --mcp-server-url TEXT           MCP server URL for OAuth callbacks (can
                                  also use NEXTCLOUD_MCP_SERVER_URL env
                                  var)  [default: http://localhost:8000]
```

### Server Options

```bash
Options:
  -h, --host TEXT                 Server host  [default: 127.0.0.1]
  -p, --port INTEGER              Server port  [default: 8000]
  -w, --workers INTEGER           Number of worker processes
  -r, --reload                    Enable auto-reload
  -l, --log-level [critical|error|warning|info|debug|trace]
                                  Logging level  [default: info]
  -t, --transport [sse|streamable-http|http]
                                  MCP transport protocol  [default: sse]
```

### App Selection

```bash
Options:
  -e, --enable-app [notes|tables|webdav|calendar|contacts|deck]
                                  Enable specific Nextcloud app APIs. Can
                                  be specified multiple times. If not
                                  specified, all apps are enabled.
```

### Example CLI Usage

```bash
# OAuth mode with custom client and port
uv run nextcloud-mcp-server --oauth \
  --oauth-client-id abc123 \
  --oauth-client-secret xyz789 \
  --port 8080

# BasicAuth mode with specific apps only
uv run nextcloud-mcp-server --no-oauth \
  --enable-app notes \
  --enable-app calendar
```

---

## Configuration Best Practices

### For Development

- Use BasicAuth for quick setup and testing
- Or use OAuth with auto-registration (dynamic client registration)
- Store `.env` file in your project directory
- Add `.env` to `.gitignore`

### For Production

- **Always use OAuth2/OIDC** with pre-configured clients
- Store OAuth client credentials securely
- Use environment variables from your deployment platform (Docker secrets, Kubernetes ConfigMaps, etc.)
- Never commit credentials to version control
- Set appropriate file permissions on credential storage:
  ```bash
  chmod 600 .nextcloud_oauth_client.json
  ```

### For Docker

- Mount OAuth client storage as a volume for persistence:
  ```bash
  docker run -v $(pwd)/.oauth:/app/.oauth --env-file .env \
    ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
  ```
- Use Docker secrets for sensitive values in production

---

## See Also

- [OAuth Setup Guide](oauth-setup.md) - Step-by-step OAuth configuration
- [Authentication](authentication.md) - Authentication modes comparison
- [Running the Server](running.md) - Starting the server with different configurations
- [Troubleshooting](troubleshooting.md) - Common configuration issues
