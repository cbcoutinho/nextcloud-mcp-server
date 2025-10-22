#!/bin/bash

set -euox pipefail

echo "Installing and configuring user_oidc app for testing..."

# Enable the user_oidc app (OIDC client for bearer token validation)
php /var/www/html/occ app:enable user_oidc

# Configure user_oidc to validate bearer tokens from the OIDC Identity Provider
php /var/www/html/occ config:system:set user_oidc oidc_provider_bearer_validation --value=true --type=boolean

patch -u /var/www/html/custom_apps/user_oidc/lib/User/Backend.php -i /docker-entrypoint-hooks.d/post-installation/0001-Fix-Bearer-token-authentication-causing-session-logo.patch
