#!/bin/bash
set -e

echo "Installing and configuring OIDC app for testing..."

# Enable the OIDC app
php /var/www/html/occ app:enable oidc

# Configure OIDC for testing with dynamic client registration enabled
# Note: The correct config key is 'dynamic_client_registration', not 'allow_dynamic_client_registration'
php /var/www/html/occ config:app:set oidc dynamic_client_registration --value='true'

echo "OIDC app installed and configured successfully"
