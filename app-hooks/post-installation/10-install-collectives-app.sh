#!/bin/bash

set -euox pipefail

echo "Installing and configuring collectives app for testing..."

# Collectives depends on Circles (teams) - ensure it's enabled
# Circles is bundled with Nextcloud, so just enable it
php /var/www/html/occ app:enable circles

# Check if development collectives app is mounted at /opt/apps/collectives
if [ -d /opt/apps/collectives ]; then
    echo "Development collectives app found at /opt/apps/collectives"

    # Remove any existing collectives app in apps (from app store or old symlink)
    if [ -e /var/www/html/custom_apps/collectives ]; then
        echo "Removing existing collectives in apps..."
        rm -rf /var/www/html/custom_apps/collectives
    fi

    # Create symlink from apps to the mounted development version
    # Per Nextcloud docs: apps outside server root need symlinks in server root
    echo "Creating symlink: custom_apps/collectives -> /opt/apps/collectives"
    ln -sf /opt/apps/collectives /var/www/html/custom_apps/collectives

    echo "Enabling collectives app from /opt/apps (development mode via symlink)"
    php /var/www/html/occ app:enable collectives
elif [ -d /var/www/html/custom_apps/collectives ]; then
    echo "collectives app directory found in apps (already installed)"
    php /var/www/html/occ app:enable collectives
else
    echo "collectives app not found, installing from app store..."
    php /var/www/html/occ app:install collectives
    php /var/www/html/occ app:enable collectives
fi
