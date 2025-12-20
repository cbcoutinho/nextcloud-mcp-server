#!/bin/bash

set -euox pipefail

echo "Installing Astrolabe app for testing..."

# Check if development astrolabe app is mounted at /opt/apps/astrolabe
if [ -d /opt/apps/astrolabe ]; then
    echo "Development astrolabe app found at /opt/apps/astrolabe"

    # Remove any existing astrolabe app in custom_apps (from app store or old symlink)
    if [ -e /var/www/html/custom_apps/astrolabe ]; then
        echo "Removing existing astrolabe in custom_apps..."
        rm -rf /var/www/html/custom_apps/astrolabe
    fi

    # Create symlink from custom_apps to the mounted development version
    # Per Nextcloud docs: apps outside server root need symlinks in server root
    echo "Creating symlink: custom_apps/astrolabe -> /opt/apps/astrolabe"
    ln -sf /opt/apps/astrolabe /var/www/html/custom_apps/astrolabe

    echo "Enabling astrolabe app from /opt/apps (development mode via symlink)"
    php /var/www/html/occ app:enable astrolabe
elif [ -d /var/www/html/custom_apps/astrolabe ]; then
    echo "astrolabe app directory found in custom_apps (already installed)"
    php /var/www/html/occ app:enable astrolabe
else
    echo "astrolabe app not found, installing from app store..."
    php /var/www/html/occ app:install astrolabe
    php /var/www/html/occ app:enable astrolabe
fi

echo "✓ Astrolabe app installed successfully"
echo ""
echo "Note: MCP server configuration is managed dynamically during tests"
echo "      to support testing multiple MCP server deployments."
