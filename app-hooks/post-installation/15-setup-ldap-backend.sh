#!/bin/bash
#
# Configure Nextcloud's user_ldap backend against the `openldap` service.
#
# Runs only when the `ldap` docker-compose profile is active (the `openldap`
# hostname resolves). Sets up a single LDAP config so the seeded user `alice`
# logs in as `alice` but is mapped to a canonical UID derived from her LDAP
# entryUUID — the divergent loginName/UID condition that reproduces GH #980.
#
# Idempotent: reuses an existing config id if present, else creates one.
#

set -e

echo "===================================================================="
echo "Configuring user_ldap backend for OpenLDAP..."
echo "===================================================================="

# Quick check: is the openldap service in the Docker network?
# When the `ldap` profile is not active, this hostname won't resolve, so the
# hook is a no-op for every other lane.
if ! getent hosts openldap >/dev/null 2>&1; then
    echo "  OpenLDAP service not detected in Docker network (ldap profile not active)"
    echo "  Skipping user_ldap configuration"
    exit 0
fi

php /var/www/html/occ app:enable user_ldap

# Reuse an existing config id (idempotent re-run), else create a fresh one.
# The trailing `|| true` guards the whole command substitution under `set -e`
# (grep exits non-zero when no config exists yet on a fresh install); keep it on
# this pipeline if refactoring.
CONFIG_ID=$(php /var/www/html/occ ldap:show-config 2>/dev/null \
    | grep -E '^\| Configuration' | grep -oE 's[0-9]+' | head -n1 || true)
if [ -z "$CONFIG_ID" ]; then
    CONFIG_ID=$(php /var/www/html/occ ldap:create-empty-config | grep -oE 's[0-9]+' | head -n1)
fi
echo "Using user_ldap config: $CONFIG_ID"

set_cfg() { php /var/www/html/occ ldap:set-config "$CONFIG_ID" "$1" "$2" >/dev/null; }

# Connection (openldap = internal docker hostname; admin bind = uid=admin).
set_cfg ldapHost openldap
set_cfg ldapPort 389
set_cfg ldapAgentName "uid=admin,dc=example,dc=org"
set_cfg ldapAgentPassword "ldap_admin_pw"
set_cfg ldapBase "dc=example,dc=org"
set_cfg ldapBaseUsers "ou=people,dc=example,dc=org"
# Filters + attribute mapping. Leaving the internal-username attribute at its
# default makes Nextcloud derive the UID from entryUUID (below), so loginName
# (uid=alice) differs from the canonical UID — the point of this lane.
set_cfg ldapUserFilter "(objectClass=inetOrgPerson)"
set_cfg ldapLoginFilter "(&(objectClass=inetOrgPerson)(uid=%uid))"
set_cfg ldapUserDisplayName "cn"
set_cfg ldapEmailAttribute "mail"
set_cfg ldapExpertUUIDUserAttr "entryUUID"
set_cfg ldapTLS 0
set_cfg turnOffCertCheck 1
set_cfg ldapConfigurationActive 1

# Wait for OpenLDAP to answer by retrying the connection test itself — no extra
# client tooling needed in the app image. This doubles as validation.
echo "Verifying LDAP connection..."
MAX_RETRIES=30
RETRY_COUNT=0
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if php /var/www/html/occ ldap:test-config "$CONFIG_ID" 2>/dev/null | grep -q "valid"; then
        echo "===================================================================="
        echo "✓ user_ldap backend configured ($CONFIG_ID)"
        echo "===================================================================="
        exit 0
    fi
    echo "  Waiting for OpenLDAP... (attempt $((RETRY_COUNT + 1))/$MAX_RETRIES)"
    sleep 5
    RETRY_COUNT=$((RETRY_COUNT + 1))
done

echo "⚠ Warning: LDAP connection could not be verified after $MAX_RETRIES attempts"
php /var/www/html/occ ldap:test-config "$CONFIG_ID" || true
exit 0
