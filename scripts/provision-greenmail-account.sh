#!/bin/bash
#
# Provision a GreenMail-backed Nextcloud Mail account for the single-user test
# user so the mail integration tests have a real account to exercise.
#
# GreenMail runs with `-Dgreenmail.auth.disabled`, so any IMAP/SMTP login
# succeeds and the mailbox is auto-created — the password below is arbitrary.
# Plaintext ports: IMAP 3143, SMTP 3025 (ssl-mode "none"). Idempotent: skips if
# the user already has a mail account.
#
# Usage: scripts/provision-greenmail-account.sh [user_id] [email]
set -euo pipefail

USER_ID="${1:-admin}"
EMAIL="${2:-${USER_ID}@example.org}"
GREENMAIL_READINESS_URL="${GREENMAIL_READINESS_URL:-http://localhost:8085/api/service/readiness}"

echo "Waiting for GreenMail readiness at ${GREENMAIL_READINESS_URL} ..."
ready=0
for _ in $(seq 1 30); do
    if curl -fsS "${GREENMAIL_READINESS_URL}" >/dev/null 2>&1; then
        echo "GreenMail is ready"
        ready=1
        break
    fi
    sleep 2
done
if [ "${ready}" -ne 1 ]; then
    echo "WARNING: GreenMail did not become ready in time; mail:account:create" \
         "may fail to reach the IMAP/SMTP server." >&2
fi

# Idempotency: mail:account:export prints a human-readable "Account N:" block
# per existing account (and nothing matching when the user has none).
if docker compose exec -T app php occ mail:account:export "${USER_ID}" 2>/dev/null | grep -qiE '^Account [0-9]+:'; then
    echo "Mail account already exists for ${USER_ID}; skipping provisioning"
    exit 0
fi

echo "Creating GreenMail mail account for ${USER_ID} (${EMAIL}) ..."
docker compose exec -T app php occ mail:account:create \
    "${USER_ID}" "${USER_ID} (GreenMail)" "${EMAIL}" \
    greenmail 3143 none "${EMAIL}" greenmail-test-pw \
    greenmail 3025 none "${EMAIL}" greenmail-test-pw

echo "Provisioned GreenMail mail account for ${USER_ID}"
