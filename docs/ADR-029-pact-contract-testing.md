# ADR-029: Pact contract testing with astrolabe

## Status

Accepted — 2026-06-10

## Context

`nextcloud-mcp-server` (Python/FastMCP) and the `astrolabe` Nextcloud app (PHP)
integrate over HTTP **in both directions**:

- **MCP server → astrolabe** (one call): the background vector-sync reads a
  user's provisioning **status** via
  `GET /apps/astrolabe/api/v1/background-sync/credentials/{user_id}`
  (`nextcloud_mcp_server/auth/astrolabe_client.py`). This returns status only —
  `{success, user_id, has_background_access, sync_type, provisioned_at}`, always
  HTTP 200, and **never the password**. The app password itself flows the other
  way: astrolabe pushes it to the MCP server's `/api/v1/users/{uid}/app-password`.
- **astrolabe → MCP server** (~10 calls): astrolabe's `McpServerClient` consumes
  this server's `/api/v1/*` HTTP API — `status`, `vector-sync/status`, `search`,
  `vector-viz/search`, `webhooks` (GET/POST/DELETE), `apps`, `chunk-context`,
  `pdf-preview` (`nextcloud_mcp_server/app.py` route table, `api/webhooks.py`).

Today nothing guarantees the two stay wire-compatible. A renamed JSON field or a
changed status code on either side is only caught — if at all — by the heavy
integration matrix (docker-compose, real Nextcloud, minutes per run). We want a
fast, focused signal that fails the moment the contract drifts, runnable in
unit-style CI on either repo independently.

## Decision

Adopt **consumer-driven contract testing with Pact**, with a self-hosted **Pact
Broker in the homelab** as the system of record. GitHub Actions reach the broker
**over Tailscale** (the broker is not publicly exposed).

Because the integration is bidirectional, each repo plays **both** Pact roles:

| Contract | Consumer | Provider | Consumer tooling | Provider tooling |
|----------|----------|----------|------------------|------------------|
| credentials API | nextcloud-mcp-server | astrolabe | `pact-python` | `pact-php` verifier |
| `/api/v1/*` API | astrolabe | nextcloud-mcp-server | `pact-php` | `pact-python` `Verifier` |

### This repo (Python side)

- `pact-python` (v3 API, `from pact import Pact, Verifier, match`) is a **dev**
  dependency.
- Contract tests live under `tests/contract/` behind a `contract` pytest marker,
  kept out of the default `unit` run.
  - **Consumer** (`test_astrolabe_credentials_consumer.py`): drives the real
    `AstrolabeClient` against a Pact mock server, pinning the request shape and
    the two status responses it branches on — provisioned
    (`has_background_access: true`, `sync_type: "app_password"`, integer
    `provisioned_at`) and unprovisioned (`has_background_access: false`). The
    OAuth token fetch is stubbed so only the credentials call is exercised.
    Interactions merge into `tests/contract/pacts/` (git-ignored — pacts are
    published to the broker, not committed).
  - **Provider** (`test_mcp_provider_verification.py`): a `Verifier` harness that
    replays astrolabe's published pacts against a running MCP server. It is
    **environment-gated** (`PACT_PROVIDER_URL` + a pact source) so it skips in
    the consumer-only job and in local runs. Provider-state handlers are
    registered by `given(...)` string in `_PROVIDER_STATES` and filled in as
    astrolabe publishes its consumer pacts; unknown states no-op so state-less
    interactions (`/api/v1/status`, `/api/v1/vector-sync/status`) verify
    immediately.

### CI (`.github/workflows/pact.yml`)

Three jobs, each joining the tailnet with the shared `tag:github-runner` OAuth
client before touching the broker:

1. **consumer** — generate pacts, publish to the broker tagged with the branch
   and commit SHA.
2. **provider** — stand up the MCP server (single-user compose profile), verify
   astrolabe's pacts against it, publish verification results (master only).
3. **can-i-deploy** — gate `master` on `pact-broker can-i-deploy … --pacticipant
   nextcloud-mcp-server --to-environment production`.

Broker-dependent steps are skipped when `PACT_BROKER` is unset (forks).

### Infrastructure (other repos / sessions)

- **Broker**: `pactfoundation/pact-broker` deployed via ArgoCD
  (`homelab-argocd`), backed by the shared Zalando Postgres, reachable at
  `pact-broker.internal.coutinho.io`. Single basic-auth credential from AWS
  Secrets Manager via external-secrets.
- **CI access**: jobs join the tailnet (`tag:github-runner`) and reach the
  broker via the `PACT_BROKER` secret (its Tailscale host). No new Terraform is
  required.

### Required secrets (this repo)

`TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET` (Tailscale github-runner OAuth client),
`PACT_BROKER` (base URL), `PACT_USERNAME`, `PACT_PASSWORD` (broker basic auth).

## Consequences

- **Fast, independent signal**: either side detects an incompatible change in a
  ~seconds-long job instead of waiting on the integration matrix.
- **Participant names are load-bearing**: the consumer/provider names
  (`nextcloud-mcp-server`, `astrolabe`) must match exactly across both repos and
  the broker. They live in `tests/contract/conftest.py` here and must mirror the
  astrolabe pact tests.
- **Provider states are deferred work**: the `/api/v1/*` provider verification is
  only as complete as the state handlers that seed its backends (webhooks DB,
  Qdrant). These land incrementally as astrolabe publishes its consumer pacts.
- **Broker is a homelab dependency**: contract publication/verification needs the
  tailnet and the broker up. Steps degrade to skipped (not failed) when the
  broker is unreachable from a fork; on `master` an outage will fail
  `can-i-deploy`.
