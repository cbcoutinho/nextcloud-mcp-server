#!/usr/bin/env bash
set -euo pipefail

# Install the unified pact CLI (Rust, pact-foundation/pact-cli) — replaces the
# deprecated pact-ruby-standalone `pact-broker` CLI (which only warns now and is
# no longer maintained). The cargo-dist installer appends its bin dir
# ($HOME/.pact/bin) to $GITHUB_PATH, so `pact` is on PATH for the broker steps
# that follow.
#
# The installer is downloaded, checksum-verified, and only then executed. It
# used to be piped straight into `sh` from three separate workflow steps, which
# executed unverified remote code and allowed a redirect to drop to plain HTTP
# (githubactions:S8482 / S6506). Renovate bumps PACT_INSTALLER_SHA256 together
# with PACT_CLI_VERSION (github-release-attachments digest). To verify by hand:
#
#   curl -fsSL https://github.com/pact-foundation/pact-cli/releases/download/<ver>/pact-installer.sh | sha256sum

# renovate: datasource=github-release-attachments depName=pact-foundation/pact-cli
PACT_CLI_VERSION="v0.10.9"
PACT_INSTALLER_SHA256="42f80e875721761abfa235ed840ff1c2010cacfc8a8193c9284a65e2632c25e1"

installer="$(mktemp)"
trap 'rm -f "$installer"' EXIT

curl --proto '=https' --tlsv1.2 -fsSL \
    "https://github.com/pact-foundation/pact-cli/releases/download/${PACT_CLI_VERSION}/pact-installer.sh" \
    -o "$installer"

echo "${PACT_INSTALLER_SHA256}  ${installer}" | sha256sum --check --strict

sh "$installer"
echo "$HOME/.pact/bin" >> "$GITHUB_PATH"
"$HOME/.pact/bin/pact" --help > /dev/null
