#!/bin/bash

set -euox pipefail

echo "Installing and configuring notes app for testing..."

# Check if development notes app is mounted at /opt/apps/notes
if [ -d /opt/apps/notes ]; then
    echo "Development notes app found at /opt/apps/notes"

    # Remove any existing notes app in apps (from app store or old symlink)
    if [ -e /var/www/html/apps/notes ]; then
        echo "Removing existing notes in apps..."
        rm -rf /var/www/html/apps/notes
    fi

    # Create symlink from apps to the mounted development version
    # Per Nextcloud docs: apps outside server root need symlinks in server root
    echo "Creating symlink: apps/notes -> /opt/apps/notes"
    ln -sf /opt/apps/notes /var/www/html/apps/notes

    echo "Enabling notes app from /opt/apps (development mode via symlink)"
    php /var/www/html/occ app:enable notes
elif [ -d /var/www/html/apps/notes ]; then
    echo "notes app directory found in apps (already installed)"
    php /var/www/html/occ app:enable notes
else
    echo "notes app not found, installing from app store..."
    php /var/www/html/occ app:install notes
    php /var/www/html/occ app:enable notes
fi
