#!/bin/sh
# Locate uvx — tries the official uv installer location first, then Homebrew, then PATH
for candidate in \
    "$HOME/.local/bin/uvx" \
    "/opt/homebrew/bin/uvx" \
    "/usr/local/bin/uvx" \
    "/home/linuxbrew/.linuxbrew/bin/uvx"; do
    if [ -x "$candidate" ]; then
        exec "$candidate" nextcloud-mcp-server run --transport stdio
    fi
done

# Fall back to whatever is in PATH
exec uvx nextcloud-mcp-server run --transport stdio
