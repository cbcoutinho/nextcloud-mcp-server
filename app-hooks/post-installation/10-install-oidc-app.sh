#!/bin/bash

set -euox pipefail

echo "Installing and configuring OIDC app for testing..."

# Enable the OIDC Identity Provider app
php /var/www/html/occ app:enable oidc

# Configure OIDC Identity Provider with dynamic client registration enabled
php /var/www/html/occ config:app:set oidc dynamic_client_registration --value='true'
php /var/www/html/occ config:app:set oidc proof_key_for_code_exchange --value=true --type=boolean

echo "OIDC app installed and configured successfully"
