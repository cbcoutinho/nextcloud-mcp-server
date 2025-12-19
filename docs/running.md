# Running the Server

This guide covers different ways to start and run the Nextcloud MCP server.

## Prerequisites

Before running the server:

1. **Install the server** - See [Installation Guide](installation.md)
2. **Configure environment** - See [Configuration Guide](configuration.md)
3. **Set up authentication** - See [OAuth Setup](oauth-setup.md) or [Authentication](authentication.md)

---

## Quick Start

Start the server using Docker:

```bash
# OAuth mode (recommended)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# BasicAuth mode
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

The server will start on `http://127.0.0.1:8000` by default.

---

## Running with Docker

### Basic Docker Run

#### OAuth Mode (Recommended)

```bash
# OAuth with auto-registration
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# OAuth with custom port
docker run -p 127.0.0.1:8080:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# OAuth with pre-configured client
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  -e NEXTCLOUD_OIDC_CLIENT_ID=abc123 \
  -e NEXTCLOUD_OIDC_CLIENT_SECRET=xyz789 \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# OAuth with specific apps only
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --enable-app notes --enable-app calendar
```

#### BasicAuth Mode (Legacy)

```bash
# BasicAuth (requires NEXTCLOUD_USERNAME/PASSWORD in .env)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest

# BasicAuth with specific apps
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest \
  --enable-app notes --enable-app webdav
```

### Docker with Persistent Token Storage

```bash
# Mount volume for persistent OAuth token storage
docker run -p 127.0.0.1:8000:8000 --env-file .env \
  -v $(pwd)/data:/app/data \
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
      - ./data:/app/data  # Persistent token storage
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
docker run -p 0.0.0.0:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# Bind to localhost only (default, more secure)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# Use a different port (map host port 8080 to container port 8000)
docker run -p 127.0.0.1:8080:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth
```

**Security Note:** Binding to `0.0.0.0` exposes the server to your network. Only use this if you understand the security implications.

### Transport Protocols

The server supports multiple MCP transport protocols:

```bash
# Streamable HTTP (default, recommended)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --transport streamable-http

# SSE - Server-Sent Events (deprecated)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --transport sse

# HTTP
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --transport http
```

> [!WARNING]
> SSE transport is deprecated and will be removed in a future version of the MCP spec. Please migrate to `streamable-http`.

### Logging

```bash
# Set log level (critical, error, warning, info, debug, trace)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --log-level debug

# Production: use warning or error
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --log-level warning
```

### Selective App Enablement

By default, all supported Nextcloud apps are enabled. You can enable specific apps only:

```bash
# Available apps: notes, tables, webdav, calendar, contacts, cookbook, deck

# Enable all apps (default)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# Enable only Notes
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --enable-app notes

# Enable multiple apps
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --enable-app notes --enable-app calendar --enable-app contacts

# Enable only WebDAV for file operations
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --enable-app webdav
```

**Use cases:**
- Reduce memory usage and startup time
- Limit functionality for security/organizational reasons
- Test specific app integrations
- Run lightweight instances with only needed features

---

## Development Mode

### Running for Development

For active development with auto-reload, mount your source code as a volume:

```bash
# Development mode with source code mounted
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  -v $(pwd):/app \
  -v $(pwd)/data:/app/data \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --log-level debug
```

For local development without Docker:

```bash
# Load environment variables
export $(grep -v '^#' .env | xargs)

# Run the server with auto-reload
uv run nextcloud-mcp-server run --oauth --log-level debug
```

### CLI Subcommands

The `nextcloud-mcp-server` CLI has two main subcommands:

1. **`run`** - Start the MCP server (default command in Docker)
   ```bash
   uv run nextcloud-mcp-server run --oauth --host 0.0.0.0 --port 8000
   ```

2. **`db`** - Database migration management (Alembic)
   ```bash
   # Show current migration revision
   uv run nextcloud-mcp-server db current

   # Upgrade to latest migration
   uv run nextcloud-mcp-server db upgrade

   # Show migration history
   uv run nextcloud-mcp-server db history

   # Create new migration (developers only)
   uv run nextcloud-mcp-server db migrate "description of changes"
   ```

### Database Migrations

Token storage uses **Alembic** for schema management:

- **Automatic migrations**: Database is upgraded automatically on server startup
- **Backward compatibility**: Pre-Alembic databases are automatically stamped with the initial revision
- **Migration files**: Located in `alembic/versions/`
- **For developers**: When changing the schema:
  1. Create a migration: `uv run nextcloud-mcp-server db migrate "add new column"`
  2. Edit the generated file in `alembic/versions/` to add SQL statements
  3. Test upgrade: `uv run nextcloud-mcp-server db upgrade`
  4. Test downgrade: `uv run nextcloud-mcp-server db downgrade`

See [Database Migrations Guide](database-migrations.md) for detailed information.

---

## Connecting to the Server

### Using MCP Inspector

MCP Inspector is a browser-based tool for testing MCP servers:

1. Start your MCP server using Docker (see above)
2. Start MCP Inspector:
   ```bash
   npx @modelcontextprotocol/inspector
   ```
3. In the browser:
   - Enter server URL: `http://localhost:8000`
   - Complete OAuth flow (if using OAuth)
   - Explore tools and resources

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

Use Docker Compose with `restart: unless-stopped` (see [Docker Compose section](#docker-compose) above).

### Monitoring Logs

```bash
# Docker (find container name first)
docker ps
docker logs -f <container-name>

# Docker Compose
docker-compose logs -f mcp
```

---

## Performance Tuning

### Production Settings

For production deployments, use Docker Compose with the recommended settings:

```yaml
version: '3.8'

services:
  mcp:
    image: ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
    command: --oauth --log-level warning --transport streamable-http
    ports:
      - "127.0.0.1:8000:8000"
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 1G
        reservations:
          cpus: '0.5'
          memory: 512M
```

### Scaling with Multiple Replicas

For higher load, use Docker Swarm or Kubernetes. See the [Helm Chart](../helm/) for Kubernetes deployments.

---

## Troubleshooting

### Server won't start

Check logs for errors:
```bash
# View container logs
docker logs <container-name>

# Or run with debug logging
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth \
  --log-level debug
```

Common issues:
- Environment variables not loaded - Check your `.env` file
- Port already in use - Use a different host port (e.g., `-p 127.0.0.1:8080:8000`)
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
