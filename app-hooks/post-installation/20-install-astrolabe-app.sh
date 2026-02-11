#!/bin/bash

set -euox pipefail

echo "Installing Astrolabe app from app store..."

if [ -d /var/www/html/custom_apps/astrolabe ]; then
    echo "astrolabe app directory found in custom_apps (already installed)"
    php /var/www/html/occ app:enable astrolabe
else
    php /var/www/html/occ app:install astrolabe
    php /var/www/html/occ app:enable astrolabe
fi

echo "Astrolabe app installed successfully"
echo ""
echo "Note: MCP server configuration is managed dynamically during tests"
echo "      to support testing multiple MCP server deployments."
