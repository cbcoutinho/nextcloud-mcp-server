# Nextcloud MCP Server

[![Docker Image](https://img.shields.io/badge/docker-ghcr.io/cbcoutinho/nextcloud--mcp--server-blue)](https://github.com/cbcoutinho/nextcloud-mcp-server/pkgs/container/nextcloud-mcp-server)

The Nextcloud MCP (Model Context Protocol) server allows Large Language Models (LLMs) like OpenAI's GPT, Google's Gemini, or Anthropic's Claude to interact with your Nextcloud instance. This enables automation of various Nextcloud actions, starting with the Notes API.

## Features

The server provides integration with multiple Nextcloud apps, enabling LLMs to interact with your Nextcloud data through a rich set of tools and resources.

## Authentication Modes

The Nextcloud MCP server supports two authentication modes:

| Mode | Status | Security | Use Case |
|------|--------|----------|----------|
| **OAuth2/OIDC** | ✅ Recommended | 🔒 High | Production deployments, multi-user scenarios |
| **Basic Auth** | ⚠️ Legacy | ⚠️ Lower | Development, backward compatibility |

### OAuth2/OIDC (Recommended)
- **Zero-config deployment** via dynamic client registration
- **No credential storage** in environment variables
- **Per-user authentication** with access tokens
- **Automatic token validation** via Nextcloud OIDC
- **Secure by design** following OAuth 2.0 standards

> [!IMPORTANT]
> **Current Implementation Limitations:**
> - Only tested with Nextcloud `user_oidc` and `oidc` apps (Nextcloud as identity provider)
> - Requires a patch for Bearer token support on non-OCS endpoints (see [docs/oauth2-bearer-token-session-issue.md](docs/oauth2-bearer-token-session-issue.md))
> - External identity providers (Azure AD, Keycloak, etc.) have not been tested
> - Dynamic client registration credentials expire (default: 1 hour) - use pre-configured clients for production

### Basic Authentication (Legacy)
- **Simple setup** with username/password
- **Single-user** server instances
- **Credentials in environment** (less secure)
- **Maintained for compatibility** - will be deprecated in future versions

**How it works:** The server automatically detects the authentication mode:
- **OAuth mode**: When `NEXTCLOUD_USERNAME` and `NEXTCLOUD_PASSWORD` are NOT set
- **BasicAuth mode**: When both username and password are provided

## Supported Nextcloud Apps

| App | Support Status | Description |
|-----|----------------|-------------|
| **Notes** | ✅ Full Support | Create, read, update, delete, and search notes. Handle attachments via WebDAV. |
| **Calendar** | ✅ Full Support | Complete calendar integration - create, update, delete events. Support for recurring events, reminders, attendees, and all-day events via CalDAV. |
| **Tables** | ⚠️ Row Operations | Read table schemas and perform CRUD operations on table rows. Table management not yet supported. |
| **Files (WebDAV)** | ✅ Full Support | Complete file system access - browse directories, read/write files, create/delete resources. |
| **Contacts** | ✅ Full Support | Create, read, update, and delete contacts and address books via CardDAV. |
| **Deck** | ✅ Full Support | Complete project management - boards, stacks, cards, labels, user assignments. Full CRUD operations and advanced features. |
| **Tasks** | ❌ [Not Started](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/73) | TBD |

Is there a Nextcloud app not present in this list that you'd like to be
included? Feel free to open an issue, or contribute via a pull-request.

## Available Tools & Resources

Resources provide read-only access to data for browsing and discovery. Unlike tools, resources are automatically listed by MCP clients and enable LLMs to explore your Nextcloud data structure.

### Core Resources
| Resource | Description |
|----------|-------------|
| `nc://capabilities` | Access Nextcloud server capabilities |
| `notes://settings` | Access Notes app settings |
| `nc://Notes/{note_id}/attachments/{attachment_filename}` | Access attachments for notes |


### Tools vs Resources

**Tools** are for actions and operations:
- Create, update, delete operations
- Structured responses with validation
- Error handling and business logic
- Examples: `deck_create_card`, `deck_update_stack`

**Resources** are for data browsing and discovery:
- Read-only access to existing data
- Automatic listing by MCP clients
- Raw data format for exploration
- Examples: `nc://Deck/boards/{board_id}`, `nc://Deck/boards/{board_id}/stacks`


## Installation

### Prerequisites

*   Python 3.11+
*   Access to a Nextcloud instance

### Local Installation

1.  Clone the repository (if running from source):
    ```shell
    git clone https://github.com/cbcoutinho/nextcloud-mcp-server.git
    cd nextcloud-mcp-server
    ```
2.  Install the package dependencies (if running via CLI):
    ```shell
    uv sync
    ```

3.  Run the CLI --help command to see all available options
    ```shell
    $ uv run nextcloud-mcp-server --help
    Usage: nextcloud-mcp-server [OPTIONS]

      Run the Nextcloud MCP server.

      Authentication Modes:
        - BasicAuth: Set NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD
        - OAuth: Leave USERNAME/PASSWORD unset (requires OIDC app enabled)

      Examples:
        # BasicAuth mode (legacy)
        $ nextcloud-mcp-server --host 0.0.0.0 --port 8000

        # OAuth mode with auto-registration   $ nextcloud-mcp-server --oauth

        # OAuth mode with pre-configured client   $ nextcloud-mcp-server
        --oauth --oauth-client-id=xxx --oauth-client-secret=yyy

    Options:
      -h, --host TEXT                 Server host  [default: 127.0.0.1]
      -p, --port INTEGER              Server port  [default: 8000]
      -w, --workers INTEGER           Number of worker processes
      -r, --reload                    Enable auto-reload
      -l, --log-level [critical|error|warning|info|debug|trace]
                                      Logging level  [default: info]
      -t, --transport [sse|streamable-http|http]
                                      MCP transport protocol  [default: sse]
      -e, --enable-app [notes|tables|webdav|calendar|contacts|deck]
                                      Enable specific Nextcloud app APIs. Can
                                      be specified multiple times. If not
                                      specified, all apps are enabled.
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
      --help                          Show this message and exit.
    ```

### Docker

A pre-built Docker image is available: `ghcr.io/cbcoutinho/nextcloud-mcp-server`

## Configuration

The server requires configuration to connect to your Nextcloud instance. Create a file named `.env` (or any name you prefer) in the directory where you'll run the server, based on the `env.sample` file.

### Option 1: OAuth2/OIDC Configuration (Recommended)

```dotenv
# .env file for OAuth mode
NEXTCLOUD_HOST=https://your.nextcloud.instance.com

# OAuth Configuration (Optional - auto-registers if not provided)
NEXTCLOUD_OIDC_CLIENT_ID=
NEXTCLOUD_OIDC_CLIENT_SECRET=
NEXTCLOUD_OIDC_CLIENT_STORAGE=.nextcloud_oauth_client.json
NEXTCLOUD_MCP_SERVER_URL=http://localhost:8000

# Leave these EMPTY for OAuth mode
NEXTCLOUD_USERNAME=
NEXTCLOUD_PASSWORD=
```

**Environment Variables:**

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEXTCLOUD_HOST` | ✅ Yes | - | Full URL of your Nextcloud instance |
| `NEXTCLOUD_OIDC_CLIENT_ID` | ⚠️ Optional | - | Pre-configured OAuth client ID (auto-registers if empty) |
| `NEXTCLOUD_OIDC_CLIENT_SECRET` | ⚠️ Optional | - | Pre-configured OAuth client secret |
| `NEXTCLOUD_OIDC_CLIENT_STORAGE` | ⚠️ Optional | `.nextcloud_oauth_client.json` | Path to store auto-registered client credentials |
| `NEXTCLOUD_MCP_SERVER_URL` | ⚠️ Optional | `http://localhost:8000` | MCP server URL for OAuth callbacks |

**Prerequisites:**
- Nextcloud OIDC app installed and enabled
- Dynamic Client Registration enabled (for auto-registration)
- See [OAuth Setup Guide](#oauth-setup-guide) below for detailed instructions

### Option 2: Basic Authentication (Legacy)

> [!WARNING]
> **Security Notice:** Basic Authentication stores credentials in environment variables and is less secure than OAuth. It's maintained for backward compatibility only and may be deprecated in future versions. Use OAuth for production deployments.

```dotenv
# .env file for BasicAuth mode
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
NEXTCLOUD_USERNAME=your_nextcloud_username
NEXTCLOUD_PASSWORD=your_app_password_or_password
```

**Environment Variables:**

*   `NEXTCLOUD_HOST`: The full URL of your Nextcloud instance.
*   `NEXTCLOUD_USERNAME`: Your Nextcloud username.
*   `NEXTCLOUD_PASSWORD`: **Important:** Use a dedicated Nextcloud App Password for security. Generate one in your Nextcloud Security settings. Alternatively, use your login password (less secure).

## OAuth Setup Guide

This guide walks you through setting up OAuth2/OIDC authentication for the Nextcloud MCP server.

### Step 1: Install Nextcloud OIDC App

1. Open your Nextcloud instance as an administrator
2. Navigate to **Apps** → **Security**
3. Find and install the **OpenID Connect user backend** app
4. Enable the app

### Step 2: Enable Dynamic Client Registration

1. Navigate to **Settings** → **OIDC** (in Administration settings)
2. Find the **Dynamic Client Registration** section
3. Enable **"Allow dynamic client registration"**
4. (Optional) Configure client expiration time:
   ```bash
   # Via Nextcloud CLI (occ) - optional, default is 3600 seconds (1 hour)
   php occ config:app:set oidc expire_time --value "86400"  # 24 hours
   ```

### Step 3: Configure MCP Server

Choose one of two approaches:

#### Approach A: Automatic Registration (Zero-config)

**Best for:** Development, testing, short-lived deployments

1. Create your `.env` file with only the host:
   ```dotenv
   NEXTCLOUD_HOST=https://your.nextcloud.instance.com
   ```

2. Start the MCP server:
   ```bash
   export $(grep -v '^#' .env | xargs)
   uv run nextcloud-mcp-server --oauth
   ```

3. The server will automatically:
   - Register a new OAuth client with Nextcloud
   - Save credentials to `.nextcloud_oauth_client.json`
   - Display registration confirmation in logs

**Note:** Dynamically registered clients expire after 1 hour by default. The server checks credentials at startup and re-registers if expired. For long-running deployments, consider Approach B.

#### Approach B: Pre-configured Client (Production)

**Best for:** Production, long-running deployments

1. Register a client via Nextcloud CLI:
   ```bash
   # SSH into your Nextcloud server
   php occ oidc:create \
     --name="Nextcloud MCP Server" \
     --type=confidential \
     --redirect-uri="http://localhost:8000/oauth/callback"

   # Note the client_id and client_secret from output
   ```

2. Add credentials to your `.env` file:
   ```dotenv
   NEXTCLOUD_HOST=https://your.nextcloud.instance.com
   NEXTCLOUD_OIDC_CLIENT_ID=your-client-id-here
   NEXTCLOUD_OIDC_CLIENT_SECRET=your-client-secret-here
   ```

3. Start the server - it will use the pre-configured credentials

**Benefits:** Pre-configured clients don't expire automatically and are more stable for production use.

### Step 4: Verify OAuth Configuration

Start the server and look for these log messages:

```
INFO     OAuth mode detected (NEXTCLOUD_USERNAME/PASSWORD not set)
INFO     Configuring MCP server for OAuth mode
INFO     Performing OIDC discovery: https://your.nextcloud.instance.com/.well-known/openid-configuration
INFO     OIDC discovery successful
INFO     OAuth client ready: <client-id>...
INFO     OAuth initialization complete
```

### Step 5: Test Authentication

The MCP server is now configured for OAuth. When clients connect:

1. Client receives OAuth authorization URL from the MCP server
2. User authenticates via browser to Nextcloud
3. Nextcloud redirects back with authorization code
4. Client exchanges code for access token
5. Client uses token to access MCP server

All API requests to Nextcloud use the user's OAuth token, ensuring proper permissions and audit trails.

## Transport Types

The server supports two transport types for MCP communication:

### Streamable HTTP (Recommended)
The `streamable-http` transport is the recommended and modern transport type that provides improved streaming capabilities:

```bash
# Use streamable-http transport (recommended)
uv run python -m nextcloud_mcp_server.app --transport streamable-http
```

### SSE (Server-Sent Events) - Deprecated
> [!WARNING]
> ⚠️ **Deprecated**: SSE transport is deprecated and will be removed in a future version of the MCP spec. SSE will be supported for the foreseable future, but users are encouraged to switch to the new transport type. Please migrate to `streamable-http`.

```bash
# SSE transport (deprecated - for backwards compatibility only)
uv run python -m nextcloud_mcp_server.app --transport sse
```

#### Docker Usage with Transports

```bash
# Using SSE transport (default - deprecated)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm ghcr.io/cbcoutinho/nextcloud-mcp-server:latest

# Using streamable-http transport (recommended)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm ghcr.io/cbcoutinho/nextcloud-mcp-server:latest \
  --transport streamable-http
```

**Note:** When using MCP clients, ensure your client supports the transport type you've configured on the server. Most modern MCP clients support streamable-http.

## Running the Server

### Locally

Ensure your environment variables are loaded, then run the server. You have several options:

#### Option 1: Using `nextcloud-mcp-server` CLI (recommended)

**OAuth Mode (Recommended):**
```bash
# Load environment variables from your .env file
export $(grep -v '^#' .env | xargs)

# Start with OAuth (auto-detected when USERNAME/PASSWORD not set)
uv run nextcloud-mcp-server --host 0.0.0.0 --port 8000

# Explicitly force OAuth mode
uv run nextcloud-mcp-server --oauth

# OAuth with custom configuration
uv run nextcloud-mcp-server --oauth \
  --oauth-client-id=your-client-id \
  --oauth-client-secret=your-client-secret

# OAuth with specific apps enabled
uv run nextcloud-mcp-server --oauth \
  --enable-app notes --enable-app calendar
```

**BasicAuth Mode (Legacy):**
```bash
# Load environment variables from your .env file (with USERNAME/PASSWORD set)
export $(grep -v '^#' .env | xargs)

# Start with BasicAuth (auto-detected when USERNAME/PASSWORD are set)
uv run nextcloud-mcp-server --host 0.0.0.0 --port 8000

# Explicitly force BasicAuth mode
uv run nextcloud-mcp-server --no-oauth

# Enable only specific Nextcloud app APIs
uv run nextcloud-mcp-server --enable-app notes --enable-app calendar

# Enable only WebDAV for file operations
uv run nextcloud-mcp-server --enable-app webdav
```

#### Option 2: Using `uvicorn`

You can also run the MCP server with `uvicorn` directly, which enables support
for all uvicorn arguments (e.g. `--reload`, `--workers`).

```bash
# Load environment variables from your .env file
export $(grep -v '^#' .env | xargs)

# Run with uvicorn using the --factory option
uv run uvicorn nextcloud_mcp_server.app:get_app --factory --reload --host 127.0.0.1 --port 8000
```

The server will start, typically listening on `http://127.0.0.1:8000`.

**Host binding options:**
- Use `--host 0.0.0.0` to bind to all interfaces
- Use `--host 127.0.0.1` to bind only to localhost (default)

See the full list of available `uvicorn` options and how to set them at [https://www.uvicorn.org/settings/]()

### Selective App Enablement

By default, all supported Nextcloud app APIs are enabled. You can selectively enable only specific apps using the `--enable-app` option:

```bash
# Available apps: notes, tables, webdav, calendar, contacts, deck

# Enable all apps (default behavior)
uv run python -m nextcloud_mcp_server.app

# Enable only Notes and Calendar
uv run python -m nextcloud_mcp_server.app --enable-app notes --enable-app calendar

# Enable only WebDAV for file operations
uv run python -m nextcloud_mcp_server.app --enable-app webdav

# Enable multiple apps by repeating the option
uv run python -m nextcloud_mcp_server.app --enable-app notes --enable-app tables --enable-app contacts
```

This can be useful for:
- Reducing memory usage and startup time
- Limiting available functionality for security or organizational reasons
- Testing specific app integrations
- Running lightweight instances with only needed features

### Using Docker

Mount your environment file when running the container:

**OAuth Mode:**
```bash
# Run with OAuth (auto-detected when USERNAME/PASSWORD not in .env)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# OAuth with persistent client storage
docker run -p 127.0.0.1:8000:8000 --env-file .env \
  -v $(pwd)/.oauth:/app/.oauth \
  --rm ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth

# OAuth with specific apps enabled
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest \
  --oauth --enable-app notes --enable-app calendar
```

**BasicAuth Mode (Legacy):**
```bash
# Run with BasicAuth (auto-detected when USERNAME/PASSWORD in .env)
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest

# Run with only specific apps enabled
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest \
  --enable-app notes --enable-app calendar

# Run with only WebDAV
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest \
  --enable-app webdav
```

This will start the server and expose it on port 8000 of your local machine.

**Note for OAuth:** When using OAuth with Docker, ensure the `NEXTCLOUD_MCP_SERVER_URL` in your `.env` file matches the accessible URL of the container (e.g., `http://localhost:8000` for local development).

## Usage

Once the server is running, you can connect to it using an MCP client like `MCP Inspector`. Once your MCP server is running, launch MCP Inspector as follows:

```bash
uv run mcp dev
```

You can then connect to and interact with the server's tools and resources through your browser.

## Troubleshooting OAuth

### Issue: "OAuth mode requires NEXTCLOUD_HOST environment variable"

**Cause:** The `NEXTCLOUD_HOST` environment variable is not set or empty.

**Solution:**
```bash
# Ensure NEXTCLOUD_HOST is set in your .env file
echo "NEXTCLOUD_HOST=https://your.nextcloud.instance.com" >> .env

# Load environment variables
export $(grep -v '^#' .env | xargs)
```

### Issue: "OAuth mode requires either client credentials OR dynamic client registration"

**Cause:** The Nextcloud OIDC app either:
1. Is not installed
2. Doesn't have dynamic client registration enabled
3. Isn't providing a registration endpoint

**Solution:**
1. Verify OIDC app is installed: Navigate to Nextcloud **Apps** → **Security**
2. Enable dynamic client registration:
   - Go to **Settings** → **OIDC** (Administration)
   - Enable "Allow dynamic client registration"
3. Or provide pre-configured credentials:
   ```dotenv
   NEXTCLOUD_OIDC_CLIENT_ID=your-client-id
   NEXTCLOUD_OIDC_CLIENT_SECRET=your-client-secret
   ```

### Issue: "Stored client has expired"

**Cause:** Dynamically registered OAuth clients expire (default: 1 hour).

**Solution:**

**Option 1:** Restart the server - it will automatically re-register
```bash
# Server checks credentials at startup and re-registers if expired
uv run nextcloud-mcp-server --oauth
```

**Option 2:** Use pre-configured credentials (recommended for production)
```bash
# Register permanent client via Nextcloud CLI
php occ oidc:create \
  --name="Nextcloud MCP Server" \
  --type=confidential \
  --redirect-uri="http://localhost:8000/oauth/callback"

# Add to .env
NEXTCLOUD_OIDC_CLIENT_ID=<from-output>
NEXTCLOUD_OIDC_CLIENT_SECRET=<from-output>
```

**Option 3:** Increase expiration time
```bash
# Via Nextcloud occ command
php occ config:app:set oidc expire_time --value "86400"  # 24 hours
```

### Issue: "HTTP 401 Unauthorized" when calling Nextcloud APIs

**Cause:** OAuth tokens may not work with certain Nextcloud endpoints due to CORS middleware session handling.

**Solution:** This is a known issue with the Nextcloud OIDC app. See [docs/oauth2-bearer-token-session-issue.md](docs/oauth2-bearer-token-session-issue.md) for details and workarounds.

The issue affects app-specific APIs (like Notes) but not OCS APIs. A patch for the `user_oidc` app is available in the documentation.

### Issue: "Permission denied" when reading/writing client credentials file

**Cause:** The server cannot access the OAuth client storage file.

**Solution:**
```bash
# Check file permissions
ls -la .nextcloud_oauth_client.json

# Fix permissions (should be 0600)
chmod 600 .nextcloud_oauth_client.json

# Ensure the directory is writable
chmod 755 $(dirname .nextcloud_oauth_client.json)
```

### Issue: Switching Between OAuth and BasicAuth

**To switch from BasicAuth to OAuth:**
```bash
# Remove or comment out USERNAME/PASSWORD in .env
# Keep only NEXTCLOUD_HOST
sed -i 's/^NEXTCLOUD_USERNAME/#NEXTCLOUD_USERNAME/' .env
sed -i 's/^NEXTCLOUD_PASSWORD/#NEXTCLOUD_PASSWORD/' .env

# Restart server with --oauth flag
uv run nextcloud-mcp-server --oauth
```

**To switch from OAuth to BasicAuth:**
```bash
# Add USERNAME/PASSWORD to .env
echo "NEXTCLOUD_USERNAME=your-username" >> .env
echo "NEXTCLOUD_PASSWORD=your-password" >> .env

# Restart server with --no-oauth flag (or let auto-detection work)
uv run nextcloud-mcp-server --no-oauth
```

### Getting Help

If you continue to experience issues:

1. **Check logs:** Run with `--log-level debug` for detailed output
   ```bash
   uv run nextcloud-mcp-server --oauth --log-level debug
   ```

2. **Verify OIDC discovery:** Check if the discovery endpoint is accessible
   ```bash
   curl https://your.nextcloud.instance.com/.well-known/openid-configuration
   ```

3. **Check dynamic registration:** Verify the endpoint exists in the discovery response
   ```json
   {
     "registration_endpoint": "https://your.nextcloud.instance.com/apps/oidc/register"
   }
   ```

4. **Open an issue:** If problems persist, open an issue on the [GitHub repository](https://github.com/cbcoutinho/nextcloud-mcp-server/issues) with:
   - Server logs (with `--log-level debug`)
   - Nextcloud version
   - OIDC app version
   - Error messages

## References:

- https://github.com/modelcontextprotocol/python-sdk

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests on the [GitHub repository](https://github.com/cbcoutinho/nextcloud-mcp-server).

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=cbcoutinho/nextcloud-mcp-server&type=Date)](https://www.star-history.com/#cbcoutinho/nextcloud-mcp-server&Date)

## License

This project is licensed under the AGPL-3.0 License. See the [LICENSE](./LICENSE) file for details.

[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/cbcoutinho-nextcloud-mcp-server-badge.png)](https://mseep.ai/app/cbcoutinho-nextcloud-mcp-server)
