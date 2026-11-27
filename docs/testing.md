# Testing guide

How the test suite is organised, what each tier costs, where a new test
belongs, and how to measure the suite. The rules in `CLAUDE.md`
(*Testing Best Practices*, *Property-based tests*) are the short form; this
page is the reasoning and the numbers behind them.

## Tiers at a glance

| Tier | Selected by | Needs | Size | Cost per test |
|---|---|---|---|---|
| Unit | `-m unit` | nothing | ~5,100 tests | median 3 ms; whole tier 2m56s serial |
| Smoke | `-m smoke` | the Docker stack | 8 | seconds |
| Integration | `-m integration` plus a mode marker | the Docker stack, per deployment mode | ~580 | p50 2.3 s, p99 ~37 s |
| Contract (Pact) | `-m contract` | a Pact broker (skips without one) | 26 | ms |
| Load | `python -m tests.load.benchmark` | the Docker stack | — | manual only |

Select unit tests **by marker, not by directory.** About 250 unit tests live
outside `tests/unit/` (`tests/client/`, `tests/test_models.py`, …).

### What CI spends (measured 2026-11-26, one `Tests` run)

| | Runner-minutes per push | Notes |
|---|---|---|
| `unit-test` job | ~3 | one job, `-n auto` |
| 12 integration lanes (6 modes × NC 33/34) | ~108 | **95% of CI** |
| of which stack setup | ~65 | `docker compose up` (Nextcloud install), Playwright, uv |
| of which tests | ~42 | |

A single integration test costs about 2.3 s, and most run in more than one
lane, so one more integration test costs CI roughly 100× what one more unit
test does. That ratio drives the rule below.

## Where does a new test belong?

Push every check to the cheapest tier that can catch the bug ("shift left"):

1. **Logic, parsing, formatting, validation** → unit. If the inputs form a
   family (any string, any payload, any id), write a Hypothesis property, not a
   `parametrize` table.
2. **Our code against a boundary we don't own** (a Nextcloud response, a
   provider's error body) → unit, with the response mocked. Parse a *recorded*
   real response and let a property vary it.
3. **Behaviour that only a real Nextcloud has** (DAV/OCS protocol semantics,
   permissions and sharing, an app's actual behaviour, the deployment-mode
   auth flow, end-to-end wiring of a tool) → integration.

Integration tests prove that the wiring works, one flow per feature. They
should not re-check what a unit test already pins: field-by-field response
parsing, input validation, error wording. When an integration test exists
only to check one of those, move the check to a unit test and delete it.

Cross-service boundaries we own also need a Pact contract test; upstream
Nextcloud apps do not (see ADR-029).

## Keeping the unit tier fast

### Settings isolation

`tests/unit/conftest.py` has an autouse fixture that restores the dynaconf
settings after each test. A reload costs ~40 ms, so the fixture reloads only
when the test left the environment or the loaded settings different from the
session baseline.

That rule took the serial unit tier from 7m20s to 2m56s (median test
56 ms → 3 ms; 2m56s → 1m35s with `-n auto` on 8 cores). Two traps found on
the way:

- pytest rewrites `PYTEST_CURRENT_TEST` for every test phase, so the env
  comparison ignores it.
- A key added with `config.set_override()` survives `reload()`. One test's
  probe key once made ~2,700 later tests reload. The fixture now removes keys
  that are absent from the baseline. It does not re-set changed keys, because
  a pinned override would beat a later test's `monkeypatch.setenv`.

The decision lives in `restore_settings()` (`tests/unit/_settings_isolation.py`),
and `tests/unit/test_settings_isolation.py` pins it: an untouched config is not
reloaded, a patched env is reloaded back to the baseline, and a leaked
override key is removed. The baseline is deep-copied, so a test that mutates a
nested setting in place still shows up as drift (today every setting is a
scalar). A *changed* key set through `set_override()` also survives the
reload. It is deliberately left alone (see above), so later tests keep
reloading until a test resets it.

Change settings in tests only through `monkeypatch.setenv(...)` followed by
`config._reload_config()`. `monkeypatch` undoes the env change, and the fixture
then sees the drift and reloads once. A test that changes `os.environ` without
`monkeypatch`, or leaves settings changed some other way, makes **every later
test** pay the reload.

### Hypothesis cost

A property runs ~100 examples in a few hundred ms (~3 ms per example), about
the cost of four to seven parametrized cases. Converting a table to a property
is roughly time-neutral; the reason to do it is the bugs it finds. Properties
belong in the unit tier only: at integration cost (seconds per example) one
property would take minutes per lane.

## Benchmarking the suite

### Unit tier, locally

```bash
# Whole tier with per-test timings
uv run pytest -m unit -q -p no:cacheprovider --junitxml=unit.xml --durations=40

# Split one file's time into setup / call / teardown
uv run pytest tests/unit/test_text_edit.py --durations=0 --durations-min=0
```

Run serially (no `-n`) when comparing two runs, on an otherwise idle machine.
JUnit's per-test `time` includes setup and teardown, so a fixture cost shows
up there. Compare medians as well as totals: a step change part-way through a
run means one test leaked state that every later test pays for.

### Traces (pytest-otel)

`pytest-otel` is a dev dependency. Point it at an OTLP collector and each
test becomes a span:

```bash
env -u TRACEPARENT \
  OTEL_EXPORTER_OTLP_ENDPOINT=<collector-host>:443 \
  uv run pytest -m unit --otel-service-name=pytest --otel-session-name=unit-<date>
```

- Pass `--otel-service-name`. The plugin otherwise reports as
  `Pytest_Otel_reporter` and ignores `OTEL_SERVICE_NAME`.
- Unset `TRACEPARENT`. If your shell inherits one (an instrumented terminal or
  agent session), the run attaches to that trace instead of starting its own.
- Find the run in Tempo with
  `{resource.service.name="pytest" && name="unit-<date>"}`.

### CI

```bash
RUN=<run id of a "Tests" workflow run>
# Job durations
gh run view $RUN --json jobs \
  --jq '.jobs[] | "\(((.completedAt|fromdateiso8601)-(.startedAt|fromdateiso8601))/60|floor)m \(.name)"'
# Step durations of every integration job
gh api repos/cbcoutinho/nextcloud-mcp-server/actions/runs/$RUN/jobs --paginate \
  --jq '.jobs[] | select(.name|startswith("integration")) | .name as $j | .steps[]
        | select(.completed_at) | [$j, .name, ((.completed_at|fromdateiso8601)-(.started_at|fromdateiso8601))] | @tsv'
```

Per-test durations of a lane come from the timestamps in its log
(`gh api --allow-escape-sequences repos/<owner>/<repo>/actions/jobs/<job>/logs`).
The single-user lane runs under xdist, so measure the gap between results of
the same `[gwN]` worker.

### An integration lane, locally

The stack reads its secrets from a gitignored `.env`. Compose interpolates
each file before merging, so `TOKEN_ENCRYPTION_KEY` (required by the base
file) cannot live in an override. Put real overrides of services in a
gitignored `docker-compose.override.yml`.

```bash
cat > .env <<EOF
TOKEN_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
LOGIN_FLOW_OIDC_CLIENT_ID=nextcloudMcpLoginFlowServer$(openssl rand -hex 4)
LOGIN_FLOW_OIDC_CLIENT_SECRET=$(openssl rand -hex 24)
EOF
# The single-user lane, as CI runs it
docker compose --profile single-user --profile mail --profile qdrant up --build -d
bash scripts/provision-greenmail-account.sh admin admin@example.org
uv run pytest -m "integration and not keycloak and not login_flow and not multi_user_basic and not ldap and not login_flow_ldap" \
  -n 4 --dist loadfile
```

Keep `LOGIN_FLOW_OIDC_CLIENT_ID` in the same format CI generates
(alphanumeric, 35 characters). A short id with hyphens made the
`27-create-login-flow-mcp-client.sh` hook fail, and Nextcloud then never
became healthy.
