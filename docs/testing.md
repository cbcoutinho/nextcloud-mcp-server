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

Test the same Nextcloud behaviour once. If a feature has an MCP-level test
(`tests/server/`), don't repeat the same request at the client level
(`tests/client/`). Keep a client test only for what the tool layer can't reach.
`tests/client/webdav/test_webdav_search.py` keeps wildcards, subdirectory scope
and XML escaping, and leaves name, type, limit and combined filters to
`tests/server/test_webdav_search_mcp.py`.

## Where integration time goes

Integration tests are slow because of request costs and indexing waits on the
dev stack. Network and pytest overhead are a small part. Measured locally on
NC 33:

| Cost | Per occurrence | What we do about it |
|---|---|---|
| A request authenticated with the login password | ~0.4 s | `nc_client` trades it for an app password at session start (~0.09 s per request) |
| Argon2 hashing of that login password at the default cost | ~0.3 s more | `01-disable-security-protections.sh` lowers the dev stack's hashing cost |
| A `DELETE` (moves to the trash) | ~0.85 s | delete the test directory, not every file in it |
| A fresh note becoming searchable | up to `VECTOR_SYNC_SCAN_INTERVAL` (30 s) | Astrolabe's sync events, run by the `astrolabe-sync-worker` service, index it in a few seconds |
| A WebDAV rewrite within the same second | the ETag doesn't change | wait 1.1 s before a rewrite whose new ETag you assert on |

Rules that follow:

- **Every request counts.** Our client strips cookies, so with the login
  password each request is a full login. Build setup through `nc_client`, not
  through a client made from `NEXTCLOUD_PASSWORD`.
- **Scope read-only setup to the module.** A fixture that writes files that
  every test only reads should be `@pytest.fixture(scope="module")`, taking
  `anyio_backend`. The WebDAV search fixtures write 8 files once per module,
  not once per test.
- **Notes are indexed by Astrolabe's delivery, not by the scanner.** Astrolabe
  queues a sync event for each note change. The `astrolabe-sync-worker`
  compose service runs the queue every second and POSTs to `MCP_SERVER_URL`'s
  `/webhooks/nextcloud`. Nextcloud's default AJAX cron would only run it on a
  page visit. Poll every second, not every five. The
  `25-configure-mcp-server-url.sh` hooks set the secret and enable only the
  `notes_sync` preset. `files_sync` would forward every file write in the
  suite, and checking each file's tags backed the queue up until a tagged-file
  test timed out in CI. Tagged files and Deck cards still wait for the scanner.
  Delivery only reaches the server named in `MCP_SERVER_URL`: each CI lane
  points it at its own server, so locally set it to the server under test
  (`http://mcp:8000` for single-user). If vector tests suddenly take ~30 s
  each, delivery has stopped and the scanner is indexing instead. Check
  `docker compose logs astrolabe-sync-worker`, and look for
  `Sync event delivery failed` in `nextcloud.log`. It names the URL it tried.
- **`nc_client` needs the login password.** It exchanges `NEXTCLOUD_PASSWORD`
  for an app password. If the variable already holds one, it uses it as is,
  and the speedup is lost. Users and groups always go through
  `NEXTCLOUD_PASSWORD` itself, because Nextcloud requires password
  confirmation for those actions, and an app password can't give it.
  A killed session leaves its token behind. It shows in the user's device list
  as "nextcloud-mcp-server integration tests".

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

## Writing properties: lessons from migrating tables

CLAUDE.md has the house rules. These are the traps we hit converting
parametrize tables, with the fix for each.

**Which tables to convert.** Convert a table whose rows are samples of a
family: any URL, any payload shape, any rejected input. Keep one whose rows
each pin a separate decision, such as one wording, one code mapping, or one
direction of a PHP/Python mismatch. A row that documents an incident becomes
an `@example` on the property.

**Trust-boundary checks** get two properties:
- *accepts exactly the valid set.* A composite draws a valid input or one
  broken by a single rule, together with the expected verdict.
  `client_id_urls` in `tests/unit/test_cimd.py` and `registrations` in
  `tests/unit/test_dcr_proxy.py` are examples.
- *raises only the documented error* over arbitrary input.

This pair found three real bugs that the example tables had missed:
- CIMD accepted a client_id with an empty trailing `#` fragment.
- The gateway's 422 parser crashed on a list body.
- A huge `expires_in_minutes` raised `OverflowError` instead of `ToolError`.

While a found bug is unfixed, pin it as a separate example test marked
`xfail(strict=True)`, which fails as a strict XPASS once the bug is fixed.
The property keeps running, and passing, over the inputs the bug doesn't
touch. Don't put the xfail on the property itself: that hides every other
input the property covers. When the fix lands, delete the xfail test, widen
the property to the inputs it used to avoid, and keep the failing input as an
`@example` on it. The three bugs above went through exactly that.

**Mirror an upstream parser differentially.** If a helper is meant to agree
with a library, compare the two directly. `test_cli_forwarded_allow_ips.py`
checks against uvicorn's `_TrustedHosts`.

**Generation traps:**
- `st.sampled_from` shrinks towards its first element, and a `st.just(x)`
  branch is drawn at most about once per run. Make the valid case an explicit
  choice (`st.sampled_from([None, *breakers])`), pin single values with
  `@example`, and read the split with `hypothesis.event()`.
- A uniform character draw almost never produces the one dangerous character.
  Bias the alphabet (`st.sampled_from("./\\%?#")`) and pin `".."`.
- `httpx2.Response(json=...)` refuses NaN and Infinity, so the test itself
  raises. For a "never raises" property over a body, build the response with
  `content=json.dumps(body)`.
- Async `@given` tests can't use the function-scoped `mocker` fixture. Drive
  the real client through `httpx2.MockTransport` instead.

**Response parsers** (shift left) get a *faithful parse* property: a generated
well-formed response parses to exactly the generated values. A parser that
documents an error type also gets *never raises except that error* over
arbitrary bodies. Where the client both writes and reads a format, add a
round-trip: `parse(create(x)) == x`. Examples are the iCal round-trip in
`tests/unit/client/test_calendar_ical_roundtrip.py` and the PROPFIND listing
in `tests/unit/client/test_webdav.py`. Once a parsing check lives here, the
integration test only needs to prove the wiring. Shared strategies
(`json_values`, `comparable_json_values`, `wire_json_values`, `header_text`)
are in `tests/strategies.py`. Import them rather than writing another
`st.recursive` JSON strategy; a local one needs a comment saying why the
shared ones do not fit.

**Pure functions:**
- **A rule that ANDs several conditions** (all keys required, every segment
  valid): break exactly one condition per example. If the strategy breaks
  one *or more*, a mutant that drops a single clause survives.
- **"Never raises" over random text** almost never reaches the success
  branch. Mix in structured-but-wrong inputs whose fields are usually in range.
  `_http_date` in `test_webdav_trash_versions.py` reaches parsing in 26% of
  draws this way.
- **A fast path against its reference** (windowed vs whole-document
  extraction) is a differential property. Cache fixtures per size with
  `functools.cache`, and give each page or item distinct content so a reordered
  or duplicated item shows.
- **A tool-level table over an input family:** move the family onto a
  property of the pure helper, and keep one tool-level case for the wiring.
- **A property that needs `tmp_path` or `monkeypatch`:** suppress
  `HealthCheck.function_scoped_fixture`, but only when every example
  overwrites the shared state it uses, and say so in the docstring. A
  `monkeypatch.setattr` in each example is undone only once, at teardown, so it
  must set the same value every time.

### State machines for ordering bugs

A store whose correctness depends on the *order* of operations (expiry, upserts
that must keep a field, first-insert-wins, purges across keys) gets a
`RuleBasedStateMachine`. Rules apply random sequences of the public operations,
and an invariant compares the store with a plain dict model after every step.
`tests/unit/test_store_state_machines.py` covers `TokenCache`,
`RefreshTokenStorage` (refresh tokens and profiles) and `BatchOcrJobStore`.
Together they take about 3 s and caught all 10 planted ordering bugs.

How to write one here:
1. **Build the machine inside a plain sync test** and run it with
   `run_state_machine_as_test(Machine, settings=...)`. Fixtures are then
   closure variables. There's no `@given`, so no function-scoped-fixture
   warning.
2. **Async stores:** use one `anyio.from_thread.start_blocking_portal()` per
   test and call `portal.call(store.method, *args)`. That keeps one event loop
   for the whole run, at about 0.5 ms per hop. `anyio.run` per call would
   create a loop every time.
3. **SQLite:** create and `initialize()` the store once per test (Alembic,
   ~230 ms), and wipe its table in the machine's `__init__` (~4 ms). Each call
   costs ~4 ms, so a database machine affords about 40 examples × 12 steps per
   second.
4. **Clock:** an explicit integer clock that a rule advances; never sleep.
   Patch only the store module's reference
   (`monkeypatch.setattr(module, "time", SimpleNamespace(time=clock.time))`),
   and reset the clock in `__init__`, because the fixture outlives each run.
5. **Make boundaries reachable.** Use tiny TTLs and 1 s ticks so that
   `expires_at == now` actually happens. Prefer one invariant that reads the
   whole store over many `get` rules that may never be picked. Start from a
   populated state with `@initialize`, because Hypothesis favours short runs.
   The first version of these machines missed half the planted bugs until all
   three changes were made, so plant a bug before trusting a machine.
6. **Budget:** explicit `settings(max_examples=..., stateful_step_count=...,
   deadline=None)`, about 1–2 s per machine.

**Mutation-check every property.** Break the code under test, see the property
fail, then restore the source. A mutant that survives means either a starved
strategy or a mutation on a branch your inputs never reach. Check which one it
is before calling the property weak.

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
