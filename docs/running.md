# Running the Server

This guide covers different ways to start and run the Nextcloud MCP server.

## Prerequisites

Before running the server:

1. **Install the server** - See [Installation Guide](installation.md)
2. **Configure environment** - See [Configuration Guide](configuration.md)
3. **Set up authentication** - See [OAuth Setup](oauth-setup.md) or [Authentication](authentication.md)

---

## Quick Start

Load your environment variables and start the server:

```bash
# Load environment variables from .env
export $(grep -v '^#' .env | xargs)

# Start the server
uv run nextcloud-mcp-server
```

The server will start on `http://127.0.0.1:8000` by default.

---

## Running Locally

### Method 1: Using nextcloud-mcp-server CLI (Recommended)

The CLI provides a simple interface with built-in defaults:

#### OAuth Mode

```bash
# Auto-detected when NEXTCLOUD_USERNAME/PASSWORD not set
uv run nextcloud-mcp-server

# Explicitly force OAuth mode
uv run nextcloud-mcp-server --oauth

# OAuth with custom host and port
uv run nextcloud-mcp-server --oauth --host 0.0.0.0 --port 8080

# OAuth with pre-configured client
uv run nextcloud-mcp-server --oauth \
  --oauth-client-id abc123 \
  --oauth-client-secret xyz789

# OAuth with specific apps only
uv run nextcloud-mcp-server --oauth \
  --enable-app notes \
  --enable-app calendar
```

#### BasicAuth Mode (Legacy)

```bash
# Auto-detected when NEXTCLOUD_USERNAME/PASSWORD are set
uv run nextcloud-mcp-server

# Explicitly force BasicAuth mode
uv run nextcloud-mcp-server --no-oauth

# BasicAuth with specific apps
uv run nextcloud-mcp-server --no-oauth \
  --enable-app notes \
  --enable-app webdav
```

### Method 2: Using uvicorn

For more control over server options (workers, reload, etc.):

```bash
# Load environment variables
export $(grep -v '^#' .env | xargs)

# Run with uvicorn
uv run uvicorn nextcloud_mcp_server.app:get_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000 \
  --reload  # Enable auto-reload for development
```

See all uvicorn options at [https://www.uvicorn.org/settings/](https://www.uvicorn.org/settings/)

### Method 3: Using Python Module

```bash
# Load environment variables
export $(grep -v '^#' .env | xargs)

# Run as Python module
python -m nextcloud_mcp_server.app --oauth --port 8000
```

---

## Running with Docker

### Basic Docker Run

```bash
# OAuth mode
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# BasicAuth mode
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

### Docker with Persistent OAuth Storage

```bash
docker run -p 127.0.0.1:8000:8000 --env-file .env \
  -v $(pwd)/.oauth:/app/.oauth \
  --rm ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth
```

### Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  mcp:
    image: ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
    command: --oauth --enable-app notes --enable-app calendar
    ports:
      - "127.0.0.1:8000:8000"
    env_file:
      - .env
    volumes:
      - ./oauth-storage:/app/.oauth
    restart: unless-stopped
```

Start the service:

```bash
# Start in foreground
docker-compose up

# Start in background
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the service
docker-compose down
```

---

## Server Options

### Host and Port

```bash
# Bind to all interfaces (accessible from network)
uv run nextcloud-mcp-server --host 0.0.0.0 --port 8000

# Bind to localhost only (default, more secure)
uv run nextcloud-mcp-server --host 127.0.0.1 --port 8000

# Use a different port
uv run nextcloud-mcp-server --port 8080
```

**Security Note:** Using `--host 0.0.0.0` exposes the server to your network. Only use this if you understand the security implications.

### Transport Protocols

The server supports multiple MCP transport protocols:

```bash
# Streamable HTTP (recommended)
uv run nextcloud-mcp-server --transport streamable-http

# SSE - Server-Sent Events (default, deprecated)
uv run nextcloud-mcp-server --transport sse

# HTTP
uv run nextcloud-mcp-server --transport http
```

> [!WARNING]
> SSE transport is deprecated and will be removed in a future version of the MCP spec. Please migrate to `streamable-http`.

### Logging

```bash
# Set log level (critical, error, warning, info, debug, trace)
uv run nextcloud-mcp-server --log-level debug

# Production: use warning or error
uv run nextcloud-mcp-server --log-level warning
```

### Selective App Enablement

By default, all supported Nextcloud apps are enabled. You can enable specific apps only:

```bash
# Available apps: notes, tables, webdav, calendar, contacts, deck

# Enable all apps (default)
uv run nextcloud-mcp-server

# Enable only Notes
uv run nextcloud-mcp-server --enable-app notes

# Enable multiple apps
uv run nextcloud-mcp-server \
  --enable-app notes \
  --enable-app calendar \
  --enable-app contacts

# Enable only WebDAV for file operations
uv run nextcloud-mcp-server --enable-app webdav
```

**Use cases:**
- Reduce memory usage and startup time
- Limit functionality for security/organizational reasons
- Test specific app integrations
- Run lightweight instances with only needed features

---

## Development Mode

For active development with auto-reload:

```bash
# Using uvicorn with reload
uv run uvicorn nextcloud_mcp_server.app:get_app \
  --factory \
  --reload \
  --host 127.0.0.1 \
  --port 8000 \
  --log-level debug
```

Or use the CLI with reload flag:

```bash
uv run nextcloud-mcp-server --reload --log-level debug
```

---

## Connecting to the Server

### Using MCP Inspector

MCP Inspector is a browser-based tool for testing MCP servers:

```bash
# Start MCP Inspector
uv run mcp dev

# In the browser:
# 1. Enter server URL: http://localhost:8000
# 2. Complete OAuth flow (if using OAuth)
# 3. Explore tools and resources
```

### Using MCP Clients

MCP clients (like Claude Desktop, LLM IDEs) can connect to your server:

1. Configure the client with your server URL
2. Complete OAuth authentication (if enabled)
3. Start interacting with Nextcloud through the LLM

---

## Verifying Server Status

### Check Server Health

```bash
# Test if server is responding
curl http://localhost:8000/health

# Expected response: HTTP 200 OK
```

### Check OAuth Configuration

Look for these log messages on startup:

**OAuth mode:**
```
INFO     OAuth mode detected (NEXTCLOUD_USERNAME/PASSWORD not set)
INFO     Configuring MCP server for OAuth mode
INFO     OIDC discovery successful
INFO     OAuth client ready: <client-id>...
INFO     OAuth initialization complete
```

**BasicAuth mode:**
```
INFO     BasicAuth mode detected (NEXTCLOUD_USERNAME/PASSWORD set)
INFO     Initializing Nextcloud client with BasicAuth
```

---

## Process Management

### Running as a Background Service

#### Using systemd (Linux)

Create `/etc/systemd/system/nextcloud-mcp.service`:

```ini
[Unit]
Description=Nextcloud MCP Server
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/nextcloud-mcp-server
EnvironmentFile=/path/to/.env
ExecStart=/path/to/uv run nextcloud-mcp-server --oauth
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable nextcloud-mcp
sudo systemctl start nextcloud-mcp
sudo systemctl status nextcloud-mcp
```

#### Using Docker Compose

See [Docker Compose section](#docker-compose) above - includes `restart: unless-stopped`.

### Monitoring Logs

```bash
# Local installation with systemd
sudo journalctl -u nextcloud-mcp -f

# Docker
docker logs -f <container-name>

# Docker Compose
docker-compose logs -f mcp
```

---

## Performance Tuning

### Multiple Workers

For production deployments with higher load:

```bash
# Using CLI (if supported)
uv run nextcloud-mcp-server --workers 4

# Using uvicorn
uv run uvicorn nextcloud_mcp_server.app:get_app \
  --factory \
  --workers 4 \
  --host 0.0.0.0 \
  --port 8000
```

### Production Settings

```bash
# Recommended production configuration
uv run nextcloud-mcp-server \
  --oauth \
  --host 127.0.0.1 \
  --port 8000 \
  --log-level warning \
  --transport streamable-http \
  --workers 2
```

---

## Troubleshooting

### Server won't start

Check logs for errors:
```bash
uv run nextcloud-mcp-server --log-level debug
```

Common issues:
- Environment variables not loaded - See [Configuration](configuration.md#loading-environment-variables)
- Port already in use - Try a different port with `--port`
- OAuth configuration errors - See [Troubleshooting](troubleshooting.md)

### Can't connect to server

1. Verify server is running: `curl http://localhost:8000/health`
2. Check firewall settings
3. Verify host binding (use `0.0.0.0` to allow network access)
4. Check OAuth authentication if enabled

### OAuth authentication fails

See [Troubleshooting OAuth](troubleshooting.md) for detailed OAuth troubleshooting.

---

## See Also

- [Configuration Guide](configuration.md) - Environment variables
- [OAuth Setup](oauth-setup.md) - OAuth authentication setup
- [Troubleshooting](troubleshooting.md) - Common issues and solutions
- [Installation](installation.md) - Installing the server
