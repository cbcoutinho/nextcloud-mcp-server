# Nextcloud MCP Server


[![Docker Image](https://img.shields.io/badge/docker-ghcr.io/cbcoutinho/nextcloud--mcp--server-blue)](https://github.com/cbcoutinho/nextcloud-mcp-server/pkgs/container/nextcloud-mcp-server)

**Enable AI assistants to interact with your Nextcloud instance.**

The Nextcloud MCP (Model Context Protocol) server allows Large Language Models like Claude, GPT, and Gemini to interact with your Nextcloud data through a secure API. Create notes, manage calendars, organize contacts, work with files, and more - all through natural language.

> [!NOTE]
> **Nextcloud has two ways to enable AI access:** Nextcloud provides [Context Agent](https://github.com/nextcloud/context_agent), an AI agent backend that powers the [Assistant](https://github.com/nextcloud/assistant) app and allows AI to interact with Nextcloud apps like Calendar, Talk, and Contacts. Context Agent runs as an ExApp inside Nextcloud and also exposes an MCP server endpoint for external LLMs. This project (Nextcloud MCP Server) is a **dedicated standalone MCP server** designed specifically for external MCP clients like Claude Code and IDEs, with deep CRUD operations and OAuth support. See our [detailed comparison](docs/comparison-context-agent.md) to understand which approach fits your use case.

## Features

### Supported Nextcloud Apps

| App | Support | Features |
|-----|---------|----------|
| **Notes** | ✅ Full | Create, read, update, delete, search notes. Handle attachments. |
| **Calendar** | ✅ Full | Manage events, recurring events, reminders, attendees via CalDAV. |
| **Contacts** | ✅ Full | CRUD operations for contacts and address books via CardDAV. |
| **Cookbook** | ✅ Full | Manage recipes with schema.org metadata. Import from URLs, search, categorize. |
| **Files (WebDAV)** | ✅ Full | Complete file system access - browse, read, write, organize files. |
| **Deck** | ✅ Full | Project management - boards, stacks, cards, labels, assignments. |
| **Tables** | ⚠️ Partial | Row-level operations. Table management not yet supported. |
| **Tasks** | ❌ Planned | [Issue #73](https://github.com/cbcoutinho/nextcloud-mcp-server/issues/73) |

Want to see another Nextcloud app supported? [Open an issue](https://github.com/cbcoutinho/nextcloud-mcp-server/issues) or contribute a pull request!

### Authentication

| Mode | Security | Best For |
|------|----------|----------|
| **OAuth2/OIDC** ⚠️ **Experimental** | 🔒 High | Testing, evaluation (requires patches) |
| **Basic Auth** ✅ | Lower | Development, testing, production |

> [!IMPORTANT]
> **OAuth is experimental** and requires manual patches to upstream Nextcloud apps. Specifically:
> - **Required patch**: `user_oidc` app needs modifications for Bearer token support ([issue #1221](https://github.com/nextcloud/user_oidc/issues/1221))
> - **Impact**: Without the patch, most app-specific APIs (Notes, Calendar, Contacts, Deck, etc.) will fail with 401 errors
> - **Production use**: Wait for upstream patches to be merged into official releases
>
> See [OAuth Upstream Status](docs/oauth-upstream-status.md) for detailed information on required patches and workarounds.

OAuth2/OIDC provides secure, per-user authentication with access tokens. See [Authentication Guide](docs/authentication.md) for details.

## Quick Start

### 1. Install

```bash
# Clone the repository
git clone https://github.com/cbcoutinho/nextcloud-mcp-server.git
cd nextcloud-mcp-server

# Install with uv (recommended)
uv sync

# Or using Docker
docker pull ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

See [Installation Guide](docs/installation.md) for detailed instructions.

### 2. Configure

Create a `.env` file:

```bash
# Copy the sample
cp env.sample .env
```

**For Basic Auth (recommended for most users):**
```dotenv
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
NEXTCLOUD_USERNAME=your_username
NEXTCLOUD_PASSWORD=your_app_password
```

**For OAuth (experimental - requires patches):**
```dotenv
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
```

See [Configuration Guide](docs/configuration.md) for all options.

### 3. Set Up Authentication

**Basic Auth Setup (recommended):**
1. Create an app password in Nextcloud (Settings → Security → Devices & sessions)
2. Add credentials to `.env` file
3. Start the server

**OAuth Setup (experimental):**
1. Install Nextcloud OIDC apps (`oidc` + `user_oidc`)
2. **Apply required patches** to `user_oidc` app (see [OAuth Upstream Status](docs/oauth-upstream-status.md))
3. Enable dynamic client registration
4. Configure Bearer token validation
5. Start the server

See [OAuth Quick Start](docs/quickstart-oauth.md) for 5-minute setup or [OAuth Setup Guide](docs/oauth-setup.md) for detailed instructions.

### 4. Run the Server

```bash
# Load environment variables
export $(grep -v '^#' .env | xargs)

# Start with Basic Auth (default)
uv run nextcloud-mcp-server

# Or start with OAuth (experimental - requires patches)
uv run nextcloud-mcp-server --oauth

# Or with Docker
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

The server starts on `http://127.0.0.1:8000` by default.

See [Running the Server](docs/running.md) for more options.

### 5. Connect an MCP Client

Test with MCP Inspector:

```bash
uv run mcp dev
```

Or connect from:
- Claude Desktop
- Any MCP-compatible client

## Documentation

### Getting Started
- **[Installation](docs/installation.md)** - Install the server
- **[Configuration](docs/configuration.md)** - Environment variables and settings
- **[Authentication](docs/authentication.md)** - OAuth vs BasicAuth
- **[Running the Server](docs/running.md)** - Start and manage the server

### Architecture
- **[Comparison with Context Agent](docs/comparison-context-agent.md)** - How this MCP server differs from Nextcloud's Context Agent

### OAuth Documentation (Experimental)
- **[OAuth Quick Start](docs/quickstart-oauth.md)** - 5-minute setup guide
- **[OAuth Setup Guide](docs/oauth-setup.md)** - Detailed setup instructions
- **[OAuth Architecture](docs/oauth-architecture.md)** - How OAuth works
- **[OAuth Troubleshooting](docs/oauth-troubleshooting.md)** - OAuth-specific issues
- **[Upstream Status](docs/oauth-upstream-status.md)** - **Required patches and PRs** ⚠️

### Reference
- **[Troubleshooting](docs/troubleshooting.md)** - Common issues and solutions

### App-Specific Documentation
- [Notes API](docs/notes.md)
- [Calendar (CalDAV)](docs/calendar.md)
- [Contacts (CardDAV)](docs/contacts.md)
- [Cookbook](docs/cookbook.md)
- [Deck](docs/deck.md)
- [Tables](docs/table.md)
- [WebDAV](docs/webdav.md)

## MCP Tools & Resources

The server exposes Nextcloud functionality through MCP tools (for actions) and resources (for data browsing).

### Tools
Tools enable AI assistants to perform actions:
- `nc_notes_create_note` - Create a new note
- `nc_cookbook_import_recipe` - Import recipes from URLs with schema.org metadata
- `deck_create_card` - Create a Deck card
- `nc_calendar_create_event` - Create a calendar event
- `nc_contacts_create_contact` - Create a contact
- And many more...

### Resources
Resources provide read-only access to Nextcloud data:
- `nc://capabilities` - Server capabilities
- `cookbook://version` - Cookbook app version info
- `nc://Deck/boards/{board_id}` - Deck board data
- `notes://settings` - Notes app settings
- And more...

Run `uv run nextcloud-mcp-server --help` to see all available options.

## Examples

### Create a Note
```
AI: "Create a note called 'Meeting Notes' with today's agenda"
→ Uses nc_notes_create_note tool
```

### Manage Recipes
```
AI: "Import the recipe from this URL: https://www.example.com/recipe/chocolate-cake"
→ Uses nc_cookbook_import_recipe tool to extract schema.org metadata
```

### Manage Calendar
```
AI: "Schedule a team meeting for next Tuesday at 2pm"
→ Uses nc_calendar_create_event tool
```

### Organize Files
```
AI: "Create a folder called 'Project X' and move all PDFs there"
→ Uses WebDAV tools (nc_webdav_create_directory, nc_webdav_move)
```

### Project Management
```
AI: "Create a new Deck board for Q1 planning with Todo, In Progress, and Done stacks"
→ Uses deck_create_board and deck_create_stack tools
```

## Transport Protocols

The server supports multiple MCP transport protocols:

- **streamable-http** (recommended) - Modern streaming protocol
- **sse** (default, deprecated) - Server-Sent Events for backward compatibility
- **http** - Standard HTTP protocol

```bash
# Use streamable-http (recommended)
uv run nextcloud-mcp-server --transport streamable-http
```

> [!WARNING]
> SSE transport is deprecated and will be removed in a future MCP specification version. Please migrate to `streamable-http`.

## Contributing

Contributions are welcome!

- Report bugs or request features: [GitHub Issues](https://github.com/cbcoutinho/nextcloud-mcp-server/issues)
- Submit improvements: [Pull Requests](https://github.com/cbcoutinho/nextcloud-mcp-server/pulls)
- Read [CLAUDE.md](CLAUDE.md) for development guidelines

## Security

[![MseeP.ai Security Assessment](https://mseep.net/pr/cbcoutinho-nextcloud-mcp-server-badge.png)](https://mseep.ai/app/cbcoutinho-nextcloud-mcp-server)

This project takes security seriously:
- OAuth2/OIDC support (experimental - requires upstream patches)
- Basic Auth with app-specific passwords (recommended)
- No credential storage with OAuth mode
- Per-user access tokens
- Regular security assessments

Found a security issue? Please report it privately to the maintainers.

## License

This project is licensed under the AGPL-3.0 License. See [LICENSE](./LICENSE) for details.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=cbcoutinho/nextcloud-mcp-server&type=Date)](https://www.star-history.com/#cbcoutinho/nextcloud-mcp-server&Date)

## References

- [Model Context Protocol](https://github.com/modelcontextprotocol)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Nextcloud](https://nextcloud.com/)
