#!/bin/bash

set -euox pipefail

php /var/www/html/occ config:system:set trusted_domains 2 --value=host.docker.internal

# Set overwrite.cli.url to the external URL for OIDC discovery
# This ensures OAuth flows redirect to the correct external URL
# Important: The Astrolabe OAuth controller makes internal HTTP requests to /.well-known/openid-configuration
# which needs to return URLs reachable by external browsers (localhost:8080, not localhost:80)
php /var/www/html/occ config:system:set overwrite.cli.url --value="http://localhost:8080"
