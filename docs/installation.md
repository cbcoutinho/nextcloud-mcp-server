# Installation

This guide covers installing the Nextcloud MCP server on your system.

## Prerequisites

- **Python 3.11+** - Check with `python3 --version`
- **Access to a Nextcloud instance** - Self-hosted or cloud-hosted
- **Administrator access** (for OAuth setup) - Required to install OIDC app

## Installation Methods

Choose one of the following installation methods:

- [From Source (Recommended)](#from-source-recommended)
- [Using Docker](#using-docker)

---

## From Source (Recommended)

Install from the GitHub repository using uv or pip.

### Prerequisites

Install [uv](https://github.com/astral-sh/uv) (recommended) or ensure pip is available:

```bash
# Install uv (recommended)
# On macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# On Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Clone the Repository

```bash
git clone https://github.com/cbcoutinho/nextcloud-mcp-server.git
cd nextcloud-mcp-server
```

### Install Dependencies

#### Using uv (Recommended)

```bash
# Install dependencies
uv sync

# Install development dependencies (optional)
uv sync --group dev
```

#### Using pip

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode
pip install -e .

# Install development dependencies (optional)
pip install -e ".[dev]"
```

### Verify Installation

```bash
# With uv
uv run nextcloud-mcp-server --help

# With pip/venv
nextcloud-mcp-server --help
```

---

## Using Docker

A pre-built Docker image is available for easy deployment.

### Pull the Image

```bash
docker pull ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

### Run the Container

```bash
# Prepare your .env file first (see Configuration guide)

# Run with environment file
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm \
  ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

### Docker Compose

Create a `docker-compose.yml`:

```yaml
version: '3.8'

services:
  mcp:
    image: ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
    ports:
      - "127.0.0.1:8000:8000"
    env_file:
      - .env
    volumes:
      # For persistent OAuth client storage
      - ./oauth-storage:/app/.oauth
    restart: unless-stopped
```

Start the service:

```bash
docker-compose up -d
```

---

## Next Steps

After installation:

1. **Configure the server** - See [Configuration Guide](configuration.md)
2. **Set up authentication** - See [OAuth Setup Guide](oauth-setup.md) or [Authentication](authentication.md)
3. **Run the server** - See [Running the Server](running.md)

## Updating

### Update from Source

```bash
cd nextcloud-mcp-server
git pull origin master

# Using uv
uv sync

# Or using pip
pip install -e .
```

### Update Docker Image

```bash
docker pull ghcr.io/cbcoutinho/nextcloud-mcp-server:latest

# If using docker-compose
docker-compose up -d  # Restart with new image

# If using docker run
# Stop the old container and start a new one with the updated image
```

## Troubleshooting Installation

### Issue: "Python version too old"

**Cause:** Python 3.11+ is required.

**Solution:**
```bash
# Check your Python version
python3 --version

# Install Python 3.11+ from:
# - https://www.python.org/downloads/
# - Or use your system package manager (apt, brew, etc.)
```

### Issue: "Command not found: nextcloud-mcp-server"

**Cause:** The package is not in your PATH.

**Solution:**
```bash
# Ensure your virtual environment is activated
source venv/bin/activate

# Or use uv run
uv run nextcloud-mcp-server --help

# Or use python -m
python -m nextcloud_mcp_server.app --help
```

### Issue: Docker permission denied

**Cause:** Docker requires elevated permissions.

**Solution:**
```bash
# Add your user to the docker group (Linux)
sudo usermod -aG docker $USER
# Log out and back in

# Or use sudo
sudo docker run ...
```

## See Also

- [Configuration Guide](configuration.md) - Environment variables and settings
- [OAuth Setup Guide](oauth-setup.md) - OAuth authentication setup
- [Running the Server](running.md) - Starting and managing the server
