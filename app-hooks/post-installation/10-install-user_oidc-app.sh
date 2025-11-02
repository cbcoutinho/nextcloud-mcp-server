#!/bin/bash

set -euox pipefail

echo "Installing and configuring user_oidc app for testing..."

# Enable the user_oidc app (OIDC client for bearer token validation)
php /var/www/html/occ app:enable user_oidc

# Configure user_oidc to validate bearer tokens from the OIDC Identity Provider
php /var/www/html/occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean
php /var/www/html/occ config:system:set user_oidc httpclient.allowselfsigned --value=true --type=boolean

# Allow Nextcloud to connect to local/internal servers (required for external IdP mode)
# This enables user_oidc to fetch JWKS from internal Keycloak container
php /var/www/html/occ config:system:set allow_local_remote_servers --value=true --type=boolean

patch -u /var/www/html/custom_apps/user_oidc/lib/User/Backend.php -i /docker-entrypoint-hooks.d/patches/0001-Fix-Bearer-token-authentication-causing-session-logo.patch
