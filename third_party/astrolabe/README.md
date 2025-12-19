# Astrolabe: The Intelligence Layer for Nextcloud

Your Nextcloud instance is more than just a bucket for files—it is a galaxy of ideas, projects, and knowledge. But until now, you've been navigating it in the dark, relying on exact filenames and rigid keywords.

**It's time to turn the lights on.**

Astrolabe is a fully integrated Nextcloud application that transforms your server into a semantic intelligence engine. It doesn't just store your data; it **maps it, understands it, and connects it** to the AI future.

---

## What You Can Do

### 🔍 Search That Actually Understands

Forget clunky external tools. Astrolabe registers as a **native Nextcloud Search Provider**.

- **Seamless**: Lives right in the standard Nextcloud search bar you already use
- **Semantic**: Type "marketing strategy for the winter launch" and Astrolabe finds the relevant PDFs, chat logs, and text files—even if those exact words never appear in the document
- **Intelligent**: It finds the **concept**, not just the string

### 🌌 Visualize Your Data Universe

Data shouldn't just be a list; it should be a landscape. Astrolabe includes a dedicated dashboard that visualizes your document chunks as a **3D PCA Vector Plot**.

- **See the Connections**: View your data as a constellation of points in 3D space
- **Explore Clusters**: Visually identify how your documents relate to one another
- **True "Astroglobe" Experience**: Rotate, zoom, and fly through your semantic universe just like navigators once studied the stars

### 🤖 Power Your AI Agents

Astrolabe isn't just for humans; it's for your AI agents, too. It acts as a bridge, running a **Model Context Protocol (MCP) Server** directly from your Nextcloud.

- **Bring Your Own Brain**: Connect external AI clients (like Claude Desktop or Cursor) to your private data
- **Agentic Workflows**: Enable LLMs to "sample" your files, read content, and perform complex reasoning tasks using your Nextcloud data as the source of truth
- **Private & Secure**: Your data never leaves your infrastructure

---

## Installation

### From App Store (Recommended)

1. Open **Apps** in your Nextcloud
2. Search for **"Astrolabe"**
3. Click **"Download and enable"**

### Manual Installation

```bash
# Clone into your Nextcloud apps directory
cd /path/to/nextcloud/apps
git clone https://github.com/cbcoutinho/nextcloud-mcp-server.git
cd nextcloud-mcp-server/third_party/astrolabe

# Install dependencies
composer install

# Enable the app
php /path/to/nextcloud/occ app:enable astrolabe
```

---

## Quick Start

### 1. Configure the MCP Server URL

Add this to your Nextcloud `config/config.php`:

```php
'mcp_server_url' => 'http://localhost:8000',
```

### 2. Start the MCP Server

The MCP server handles semantic search and AI agent connections. See the [MCP Server Installation Guide](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/installation.md) for details.

Quick start with Docker:

```bash
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest --oauth
```

### 3. Authorize Access

1. Go to **Settings → Personal → Astrolabe**
2. Click **"Authorize Access"**
3. Sign in to your identity provider
4. Approve the requested permissions

That's it! You can now use semantic search and explore your data universe.

---

## Features

### Personal Settings

Located in: **Settings → Personal → Astrolabe**

- **Semantic Search Dashboard**: Interactive 3D visualization of your document chunks
- **OAuth Authorization**: Authorize Nextcloud to access the MCP server on your behalf
- **Session Information**: View connection status and authentication details
- **Connection Management**: Revoke access or disconnect when needed

### Admin Settings

Located in: **Settings → Administration → Astrolabe**

- **Server Status**: Monitor MCP server health and version
- **Vector Sync Metrics**: See how many documents are indexed, processing rates, and sync status
- **Configuration Validation**: Verify server URL and connectivity
- **Feature Availability**: Check which capabilities are enabled

### Unified Search Integration

Astrolabe integrates directly with Nextcloud's **Unified Search**:

- Available in the top search bar across all Nextcloud pages
- Returns semantic matches ranked by relevance
- Shows excerpts from matching documents
- Links directly to source files in Nextcloud

---

## Use Cases

### For Individuals

- **Research**: Find all notes related to a project, even if they use different terminology
- **Organization**: Discover forgotten documents related to your current work
- **Exploration**: Visualize how your knowledge connects and evolves over time

### For Teams

- **Knowledge Discovery**: Surface institutional knowledge that would otherwise stay buried
- **Collaboration**: Find team members working on similar problems
- **Documentation**: Locate relevant documentation without knowing exact titles

### For Developers

- **AI Integration**: Connect Claude Desktop, Cursor, or other MCP clients to Nextcloud
- **RAG Workflows**: Build retrieval-augmented generation pipelines on your private data
- **Custom Agents**: Use the MCP protocol to create specialized workflows

---

## Requirements

- **Nextcloud**: Version 30 or later
- **MCP Server**: Running instance (Docker recommended)
- **Identity Provider**: OAuth provider supporting PKCE (Nextcloud OIDC Login or Keycloak)
- **Vector Sync**: Optional but recommended for semantic search (see [configuration guide](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/configuration.md))

---

## Documentation

### User Guides

- [MCP Server Installation](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/installation.md)
- [Configuration Guide](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/configuration.md)
- [OAuth Setup](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/oauth-setup.md)

### Technical Details

- [ADR-018: Nextcloud PHP App Architecture](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/ADR-018-nextcloud-php-app-for-settings-ui.md)
- [OAuth PKCE Flow Details](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/ADR-004-progressive-consent.md)
- [Vector Sync Architecture](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/ADR-002-vector-sync-authentication.md)

### Troubleshooting

**Cannot connect to MCP server:**
- Verify `mcp_server_url` in `config.php`
- Check MCP server is running: `curl http://localhost:8000/health`
- Review logs: `tail -f data/nextcloud.log`

**Authorization fails:**
- Ensure MCP server is in OAuth mode
- Verify identity provider is accessible
- Check browser console for errors

**Semantic search returns no results:**
- Verify vector sync is enabled and running
- Check indexing status in Admin settings
- Allow time for initial indexing to complete

For more help, see the [Troubleshooting Guide](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/docs/troubleshooting.md).

---

## Contributing

We welcome contributions! Here's how to get started:

1. Fork the [nextcloud-mcp-server repository](https://github.com/cbcoutinho/nextcloud-mcp-server)
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes in `third_party/astrolabe/`
4. Test thoroughly with a local Nextcloud instance
5. Submit a pull request

See [CONTRIBUTING.md](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/CONTRIBUTING.md) for detailed guidelines.

---

## License

AGPL-3.0

---

## About

**Astrolabe** is developed as part of the [Nextcloud MCP Server](https://github.com/cbcoutinho/nextcloud-mcp-server) project, bringing the power of semantic search and AI integration to Nextcloud.

**Author**: Chris Coutinho <chris@coutinho.io>

---

**Your Data. Mapped. Visualized. Connected.**

Install Astrolabe for Nextcloud.
