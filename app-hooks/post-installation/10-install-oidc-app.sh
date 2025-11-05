#!/bin/bash

set -euox pipefail

echo "Installing and configuring OIDC app for testing..."

# Check if development OIDC app is mounted at /opt/apps/oidc
if [ -d /opt/apps/oidc ]; then
    echo "Development OIDC app found at /opt/apps/oidc"

    # Remove any existing OIDC app in custom_apps (from app store or old symlink)
    if [ -e /var/www/html/custom_apps/oidc ]; then
        echo "Removing existing OIDC in custom_apps..."
        rm -rf /var/www/html/custom_apps/oidc
    fi

    # Create symlink from custom_apps to the mounted development version
    # Per Nextcloud docs: apps outside server root need symlinks in server root
    echo "Creating symlink: custom_apps/oidc -> /opt/apps/oidc"
    ln -sf /opt/apps/oidc /var/www/html/custom_apps/oidc

    echo "Enabling OIDC app from /opt/apps (development mode via symlink)"
    php /var/www/html/occ app:enable oidc
elif [ -d /var/www/html/custom_apps/oidc ]; then
    echo "OIDC app directory found in custom_apps (already installed)"
    php /var/www/html/occ app:enable oidc
else
    echo "OIDC app not found, installing from app store..."
    php /var/www/html/occ app:install oidc
    php /var/www/html/occ app:enable oidc
fi

# Configure OIDC Identity Provider with dynamic client registration enabled
php /var/www/html/occ config:app:set oidc dynamic_client_registration --value='true' # NOTE: String
php /var/www/html/occ config:app:set oidc proof_key_for_code_exchange --value=true --type=boolean
php /var/www/html/occ config:app:set oidc allow_user_settings --value='true' --type=boolean
php /var/www/html/occ config:app:set oidc default_token_type --value='jwt'

echo "OIDC app installed and configured successfully"
