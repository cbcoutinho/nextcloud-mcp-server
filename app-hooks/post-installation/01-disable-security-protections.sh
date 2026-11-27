#!/bin/bash
set -euox pipefail
echo "Disabling bruteforce protection and rate limiting for dev/CI..."
php /var/www/html/occ config:system:set auth.bruteforce.protection.enabled --value=false --type=boolean
php /var/www/html/occ config:system:set ratelimit.protection.enabled --value=false --type=boolean
echo "Bruteforce protection and rate limiting disabled."

# Cheap argon2id for dev/CI. NextcloudClient strips cookies (CSRF), so every
# request authenticated with a login password re-verifies it. At the default
# cost (64 MiB, t=4) that was ~1 s per request on a loaded runner and the
# largest single cost of the integration lanes. Hashes made at install time
# are re-hashed with these settings on the user's next successful login.
echo "Lowering password-hashing cost for dev/CI..."
php /var/www/html/occ config:system:set hashingMemoryCost --value=8192 --type=integer
php /var/www/html/occ config:system:set hashingTimeCost --value=1 --type=integer
