# MCP Server UI

Nextcloud app for managing the Nextcloud MCP (Model Context Protocol) Server.

## Overview

This app provides a native Nextcloud interface for managing your MCP Server, eliminating the need for the separate `/app` endpoint. It integrates seamlessly with Nextcloud's settings interface and provides:

- **Personal Settings**: Session management, background access control
- **Admin Settings**: Server status monitoring, vector sync metrics
- **Vector Visualization**: Interactive semantic search (coming soon)
- **Native Integration**: Uses Nextcloud sessions, design system, and UX patterns

## Architecture

Based on **ADR-018: Nextcloud PHP App for Settings UI**.

### OAuth PKCE Flow

The app uses **OAuth 2.0 Authorization Code flow with PKCE** (Proof Key for Code Exchange):

```
User → NC Settings → OAuth Authorize → IdP Login → Callback
    ↓
NC stores encrypted per-user tokens
    ↓
NC PHP App → User's OAuth Token → MCP Server validates
```

**Key Features**:
- ✅ **No client secrets** - Uses PKCE (public client model)
- ✅ **Per-user authorization** - Each user explicitly authorizes access
- ✅ **Encrypted token storage** - Tokens stored encrypted in NC database
- ✅ **Leverages existing validation** - MCP server uses UnifiedTokenVerifier
- ✅ **User-revocable** - Users can disconnect at any time

### Architecture Benefits

- ✅ Preserves full MCP protocol support (sampling, elicitation, streaming)
- ✅ Provides native Nextcloud integration
- ✅ Maintains clear separation of concerns
- ✅ Avoids ExApp limitations (see ADR-011)
- ✅ No shared secrets between NC and MCP server

## Requirements

1. **Nextcloud**: Version 30 or later
2. **MCP Server**: Running in OAuth mode
3. **Identity Provider**: OAuth provider supporting PKCE (Nextcloud OIDC or Keycloak)
4. **Configuration**: Set in `config.php`:

```php
'mcp_server_url' => 'http://localhost:8000',
```

## Installation

### From App Store (Recommended)

1. Open Nextcloud Apps
2. Search for "MCP Server UI"
3. Click "Download and enable"

### Manual Installation

1. Clone or download this directory to `apps/astrolabe`
2. Install dependencies: `composer install`
3. Enable the app: `occ app:enable astrolabe`

## Configuration

### 1. Configure Nextcloud

Add to `config/config.php`:

```php
'mcp_server_url' => 'http://localhost:8000',
```

### 2. Configure MCP Server

Ensure MCP server is running in OAuth mode. Add to MCP server `.env`:

```bash
# Enable OAuth mode
ENABLE_OAUTH=true

# OAuth provider (Nextcloud or Keycloak)
NEXTCLOUD_HOST=https://your-nextcloud.example.com
# OR
KEYCLOAK_SERVER_URL=https://keycloak.example.com
KEYCLOAK_REALM=nextcloud-mcp

# Optional: Disable legacy /app endpoint
ENABLE_BROWSER_UI=false
```

### 3. User Authorization

Each user must authorize the Nextcloud app to access the MCP server:

1. Go to **Settings → Personal → MCP Server**
2. Click **"Authorize Access"**
3. Sign in to your identity provider
4. Approve the requested permissions
5. You will be redirected back to Nextcloud

The app uses **OAuth 2.0 with PKCE** (Public Client) - no client secrets are stored.

## Features

### Personal Settings

Located in: **Settings → Personal → MCP Server**

- **OAuth Authorization**: Authorize Nextcloud to access MCP server on your behalf
- **Session Information**: View user ID, auth mode, OAuth connection status
- **Background Access**: Monitor whether MCP server has offline access enabled
- **IdP Profile**: View identity provider profile details
- **Connection Management**:
  - Revoke background access (removes server-side refresh token)
  - Disconnect from MCP server (removes local OAuth tokens)
- **Vector Visualization**: Access interactive semantic search UI (if enabled)

### Admin Settings

Located in: **Settings → Administration → MCP Server**

- Server status and version info
- Configuration validation (URL, API key)
- Vector sync metrics (if enabled):
  - Indexed documents count
  - Pending queue size
  - Processing rate (docs/sec)
  - Error counts
- Uptime monitoring
- Feature availability

## Development

### Structure

```
astrolabe/
├── lib/
│   ├── Controller/
│   │   ├── ApiController.php      # Form handlers (revoke, etc.)
│   │   └── PageController.php     # Main page routes
│   ├── Service/
│   │   └── McpServerClient.php    # HTTP client for MCP server API
│   └── Settings/
│       ├── Personal.php            # User settings panel
│       ├── PersonalSection.php     # Settings section
│       ├── Admin.php               # Admin settings panel
│       └── AdminSection.php        # Admin section
├── templates/
│   └── settings/
│       ├── personal.php            # Personal settings template
│       ├── admin.php               # Admin settings template
│       └── error.php               # Error template
├── css/
│   └── astrolabe-settings.css   # Settings styles
└── js/
    ├── astrolabe-personalSettings.js
    └── astrolabe-adminSettings.js
```

### Testing

1. Start MCP server with management API enabled
2. Configure Nextcloud (config.php)
3. Enable the app: `occ app:enable astrolabe`
4. Navigate to settings panels
5. Verify data loads from MCP server API

### Debugging

**Check logs:**
```bash
# Nextcloud logs
tail -f data/nextcloud.log

# MCP server logs
docker compose logs -f mcp
```

**Common issues:**

1. **"Cannot connect to MCP server"**
   - Verify `mcp_server_url` is correct in config.php
   - Check MCP server is running and accessible
   - Verify network connectivity

2. **"Authorization Required" shown on personal settings**
   - User needs to click "Authorize Access" to complete OAuth flow
   - Verify MCP server is running in OAuth mode (`ENABLE_OAUTH=true`)
   - Check identity provider is accessible

3. **OAuth callback fails**
   - Verify redirect URI is registered with IdP
   - Check MCP server OAuth configuration
   - Review browser console for errors
   - Check nextcloud.log for PHP errors

4. **Settings panel blank**
   - Check browser console for errors
   - Verify templates exist in `templates/settings/`
   - Check PHP errors in nextcloud.log

## Migration from /app Endpoint

If you're currently using the MCP server's `/app` endpoint:

1. **Phase 1** (v0.53+): Both UIs available
   - Install this app
   - Keep using `/app` or migrate to NC app
   - Test functionality in NC app

2. **Phase 2** (v0.54+): NC app recommended
   - `/app` shows deprecation notice
   - New features only in NC app
   - Begin migration

3. **Phase 3** (v0.56+): NC app only
   - `/app` endpoint removed
   - All users must use NC app

See [ADR-018](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/ADR-018-nextcloud-php-app-for-settings-ui.md) for full migration plan.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## Documentation

- [ADR-018: Nextcloud PHP App Architecture](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/ADR-018-nextcloud-php-app-for-settings-ui.md)
- [MCP Server Configuration Guide](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/configuration.md)
- [MCP Server Installation](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/installation.md)

## License

AGPL-3.0

## Author

Chris Coutinho <chris@coutinho.io>
