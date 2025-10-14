#!/bin/bash

set -euox pipefail

echo "Installing and configuring OIDC apps for testing..."

# Enable the OIDC Identity Provider app
php /var/www/html/occ app:enable oidc

# Enable the user_oidc app (OIDC client for bearer token validation)
php /var/www/html/occ app:enable user_oidc

patch -u /var/www/html/custom_apps/user_oidc/lib/User/Backend.php -i /docker-entrypoint-hooks.d/post-installation/0001-Fix-Bearer-token-authentication-causing-session-logo.patch
patch -u /var/www/html/custom_apps/oidc/lib/Util/DiscoveryGenerator.php -i /docker-entrypoint-hooks.d/post-installation/0002-Add-PKCE-code-challenge-methods-to-discovery-documen.patch

# Configure OIDC Identity Provider with dynamic client registration enabled
php /var/www/html/occ config:app:set oidc dynamic_client_registration --value='true'
php /var/www/html/occ config:app:set oidc proof_key_for_code_exchange --value=true --type=boolean

# Configure user_oidc to validate bearer tokens from the OIDC Identity Provider
php /var/www/html/occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean

echo "OIDC apps installed and configured successfully"
