# Installation

This guide covers installing the Nextcloud MCP server on your system.

## Prerequisites

- **Python 3.11+** - Check with `python3 --version`
- **Access to a Nextcloud instance** - Self-hosted or cloud-hosted
- **Administrator access** (for OAuth setup) - Required to install OIDC app

## Installation Methods

Choose one of the following installation methods:

- [Using uv (Recommended)](#using-uv-recommended)
- [Using pip](#using-pip)
- [Using Docker](#using-docker)
- [From Source](#from-source)

---

## Using uv (Recommended)

[uv](https://github.com/astral-sh/uv) is a fast Python package installer and resolver.

### Install uv

```bash
# On macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# On Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Install Nextcloud MCP Server

```bash
# Install from PyPI
uv pip install nextcloud-mcp-server

# Or install directly using uvx
uvx nextcloud-mcp-server --help
```

### Verify Installation

```bash
uv run nextcloud-mcp-server --help
```

---

## Using pip

Standard installation using pip:

```bash
# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install from PyPI
pip install nextcloud-mcp-server

# Verify installation
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

## From Source

Install from the GitHub repository:

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

# With pip
nextcloud-mcp-server --help
```

---

## Next Steps

After installation:

1. **Configure the server** - See [Configuration Guide](configuration.md)
2. **Set up authentication** - See [OAuth Setup Guide](oauth-setup.md) or [Authentication](authentication.md)
3. **Run the server** - See [Running the Server](running.md)

## Updating

### Update with uv

```bash
uv pip install --upgrade nextcloud-mcp-server
```

### Update with pip

```bash
pip install --upgrade nextcloud-mcp-server
```

### Update Docker Image

```bash
docker pull ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
docker-compose up -d  # Restart with new image
```

### Update from Source

```bash
cd nextcloud-mcp-server
git pull origin master
uv sync  # or: pip install -e .
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
