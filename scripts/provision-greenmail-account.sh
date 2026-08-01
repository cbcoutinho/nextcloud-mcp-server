#!/bin/bash
#
# Provision a GreenMail-backed Nextcloud Mail account for the single-user test
# user so the mail integration tests have a real account to exercise.
#
# GreenMail runs with `-Dgreenmail.auth.disabled`, so any IMAP/SMTP login
# succeeds and the mailbox is auto-created — the password below is arbitrary.
# Plaintext ports: IMAP 3143, SMTP 3025 (ssl-mode "none"). Idempotent: skips if
# the user already has a mail account, but always re-checks the mailboxes.
#
# GreenMail auto-creates INBOX and nothing else, which is not enough to exercise
# the write tools: `nc_mail_delete_message` needs a trash mailbox (the Mail app
# answers 400 "No trash mailbox configured" without one) and
# `nc_mail_move_message` needs somewhere to move to (its test otherwise skips
# itself for lack of a destination). So also create Trash + Archive, which the
# Mail app maps onto the `trash`/`archive` special roles by name.
#
# Usage: scripts/provision-greenmail-account.sh [user_id] [email] [password]
set -euo pipefail

USER_ID="${1:-admin}"
EMAIL="${2:-${USER_ID}@example.org}"
# Only used for the Mail HTTP API calls below (occ needs no password). The test
# stacks run as admin/admin; override for any other user.
PASSWORD="${3:-${NEXTCLOUD_PASSWORD:-admin}}"
GREENMAIL_READINESS_URL="${GREENMAIL_READINESS_URL:-http://localhost:8085/api/service/readiness}"

# Run a Mail API call from inside the `app` container, so this works regardless
# of which host port (if any) the container is published on.
#
# Credentials go in via `--config -` (stdin) rather than `-u`, so they never
# appear in the container's process list. The default here is the throwaway
# admin/admin test account, but NEXTCLOUD_PASSWORD may point this at a real one.
mail_api() {
    local method="$1" path="$2" body="${3:-}"
    printf 'user = "%s:%s"\n' "${USER_ID}" "${PASSWORD}" \
        | docker compose exec -T app curl -sS --config - -X "${method}" \
            -H 'OCS-APIRequest: true' \
            -H 'Content-Type: application/json' \
            ${body:+-d "${body}"} \
            "http://localhost/index.php/apps/mail/api/${path}"
}

# Create Trash/Archive, then opt them into background sync. Both steps matter:
# creating the mailbox is what gives the account a trash role (so delete works)
# and a move destination, but a freshly-created mailbox is not *cached*, and
# listing an uncached mailbox fails with 400 "mailbox N is not cached" — even
# after `mail:account:sync --force`. Setting `syncInBackground` is what gets it
# cached, mirroring what the Mail UI does when you first open a folder.
#
# Re-creating an existing mailbox is rejected by the Mail app rather than
# duplicating it, and curl without `-f` does not treat an HTTP error as a
# failure, so the create step is idempotent by construction.
ensure_mailboxes() {
    local account_id ids
    # `mail:account:export` prints "Account <id>:" — the same output the
    # idempotency check above greps, so no HTTP round-trip to find the id.
    account_id=$(docker compose exec -T app php occ mail:account:export "${USER_ID}" 2>/dev/null \
        | sed -nE 's/^Account ([0-9]+):.*/\1/p' | head -1)
    if [ -z "${account_id}" ]; then
        echo "WARNING: no mail account found; skipping mailbox creation" >&2
        return 0
    fi

    for name in Trash Archive; do
        echo "Ensuring ${name} mailbox on account ${account_id} ..."
        mail_api POST "mailboxes" "{\"accountId\":${account_id},\"name\":\"${name}\"}" \
            >/dev/null 2>&1 || true
    done

    # Re-read rather than using the create response: on a re-run the create is a
    # no-op error and returns no id.
    ids=$(mail_api GET "mailboxes?accountId=${account_id}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
boxes = d.get("mailboxes", d) if isinstance(d, dict) else d
print(" ".join(
    str(m["databaseId"]) for m in boxes if m.get("name") in ("Trash", "Archive")
))')
    for id in ${ids}; do
        mail_api PATCH "mailboxes/${id}" '{"syncInBackground":true}' >/dev/null 2>&1 || true
    done

    # First sync populates the message cache for the newly-tracked mailboxes.
    docker compose exec -T app php occ mail:account:sync --force "${account_id}" >/dev/null 2>&1 || true
}

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
    echo "Mail account already exists for ${USER_ID}; skipping account creation"
else
    echo "Creating GreenMail mail account for ${USER_ID} (${EMAIL}) ..."
    docker compose exec -T app php occ mail:account:create \
        "${USER_ID}" "${USER_ID} (GreenMail)" "${EMAIL}" \
        greenmail 3143 none "${EMAIL}" greenmail-test-pw \
        greenmail 3025 none "${EMAIL}" greenmail-test-pw
fi

ensure_mailboxes

echo "Provisioned GreenMail mail account for ${USER_ID}"
