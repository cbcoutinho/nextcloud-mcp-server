#!/bin/bash
set -euox pipefail
echo "Disabling bruteforce protection and rate limiting for dev/CI..."
php /var/www/html/occ config:system:set auth.bruteforce.protection.enabled --value=false --type=boolean
php /var/www/html/occ config:system:set ratelimit.protection.enabled --value=false --type=boolean
echo "Bruteforce protection and rate limiting disabled."
