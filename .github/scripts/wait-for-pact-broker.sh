#!/usr/bin/env bash
#
# Poll the Pact broker until it answers, or fail with an unambiguous message.
#
# The broker is a homelab dependency reachable only over the tailnet, and
# `tailscale up` returning does not mean MagicDNS/routes have settled. Without a
# probe, a transient connection error surfaces through the pact FFI as:
#
#   Failed to load pact - No pacts found for provider 'nextcloud-mcp-server'
#   matching the given consumer version selectors ...: IO Error - ...
#
# which reads as a contract break rather than a network blip (this reddened
# master on run 29522648078; a re-run of the same commit was green). Probing
# first both rides out the blip and, on a real outage, names the actual cause.
#
# Usage: wait-for-pact-broker.sh   (reads $PACT_BROKER; callers gate on it being set)
set -uo pipefail

: "${PACT_BROKER:?PACT_BROKER must be set}"

attempts=12
delay=5
started=$SECONDS

for i in $(seq 1 "$attempts"); do
  # -f so an HTTP error status is a failure; the broker root answers 200 to an
  # unauthenticated GET, so this probes reachability only, not credentials.
  if curl -fsS -o /dev/null -m 10 "${PACT_BROKER%/}/"; then
    echo "broker ready (attempt $i)"
    exit 0
  fi
  # No sleep after the final attempt — it would only delay the error below.
  if [ "$i" -lt "$attempts" ]; then
    echo "attempt $i/$attempts: broker not reachable; retrying in ${delay}s"
    sleep "$delay"
  fi
done

# Report measured elapsed time, not attempts*delay: each curl may burn up to its
# -m 10 timeout when the broker is reachable-but-hanging (vs failing instantly
# when it's unroutable), so the computed figure could understate reality by
# minutes. This message exists to make an outage unambiguous, so it should not
# itself state a number that never happened.
echo "::error title=Pact broker unreachable::${PACT_BROKER} did not respond after $attempts attempts over $((SECONDS - started))s. This is a tailnet/broker outage, NOT a contract failure."
exit 1
