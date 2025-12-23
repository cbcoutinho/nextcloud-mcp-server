#!/bin/bash

set -euox pipefail

php /var/www/html/occ config:system:set trusted_domains 2 --value=host.docker.internal

# Do NOT set overwritehost/overwrite.cli.url - let Nextcloud use the request's Host header
# This allows:
# - Browser requests to localhost:8080 → returns localhost:8080 URLs
# - Container requests to app:80 → returns app:80 URLs (for DCR, token exchange, etc.)
