---
name: pre-push-review
description: |
  Self-review the current branch's diff against nextcloud-mcp-server review patterns
  before pushing a PR. Runs ruff/ty/tests on changed files and produces a punch list
  of likely review-round findings, calibrated to issues that have repeatedly surfaced
  in this repo's automated PR reviews. Use when the user is about to push, says "ready
  to push", "review my work", "check before PR", or invokes /pre-push-review.
  Report-only — does not modify code.
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
---

# Pre-Push Review

## Purpose

Catch the issues that have repeatedly surfaced in PR reviews on this repo (#733–#750)
*before* the human reviewer or the automated `claude` PR-review bot sees them. Output is
a labelled punch list. The main loop decides what to fix.

This skill is calibrated to **this repo's recurring patterns** — not a generic code
review. The point is to short-circuit the review-round-N loop, not to replace human
judgment.

## When to Use

**Trigger when:**
- User says "review my work", "ready to push", "check before PR", "pre-push", or
  invokes `/pre-push-review`.
- After a substantive change (new module, new tool, refactor, behavior change) and
  before `git push`.
- Before opening or updating a PR.

**Skip when:**
- Tiny diffs (typo fix, README tweak, dependency bump only).
- User has explicitly said "just push it" / "skip the review".
- Branch is `master`, or has zero commits ahead of base **and** a clean working tree (`git status --short` empty).

## Workflow

### Phase 1 — Establish scope (≤ 30s)

Determine the base branch and diff range. Default base is `master`.

```bash
git fetch origin master --quiet
BASE=$(git merge-base HEAD origin/master)
git rev-list --count $BASE..HEAD                     # commits ahead
git diff --stat $BASE                                # files touched (incl. staged + unstaged)
git log --format="%h %s" $BASE..HEAD                 # commit list
git status --short                                   # surface uncommitted state
```

**Diff scope:** the review uses `git diff $BASE` (base → working tree), which includes
committed + staged + unstaged changes. This means in-progress work is reviewed too —
flag any findings against half-written code as such, and don't penalize obvious WIP
(missing tests, TODO stubs) the user clearly hasn't finished yet.

If the user names a different base (e.g. `main`, a stacked branch), use that instead.

**Identify the change shape** — these classifications drive scope-aware checks (Phase 3):

| Touches | Treat as |
|---|---|
| `nextcloud_mcp_server/auth/`, `*scope*`, `*token*`, `*verifier*` | **security-sensitive** (escalate B, J, K to blocking) |
| `nextcloud_mcp_server/server/*.py` (new `@mcp.tool`) | **MCP surface** (D4, I1 mandatory) |
| `nextcloud_mcp_server/client/*.py` | **client layer** (B1–B4, H1, retry/backoff) |
| `nextcloud_mcp_server/models/*.py` | **schema** (D1–D3, validators for PHP quirks) |
| `nextcloud_mcp_server/search/`, `nextcloud_mcp_server/vector/` | **vector subsystem** (E*, F3, latency budget) |
| Webhooks, `nextcloud_mcp_server/auth/webhook_*` | **webhook handler** (J3, J4, K1) |
| Tests only | **test scope** (skip A–E, run F1–F4) |
| Docs / ADR only | **docs scope** (run G3, G4 only) |

**Report up front:** branch, base, commits ahead, files changed, change shape.

### Phase 2 — Automated checks (in parallel, ≤ 30s)

Run these from the project root with separate `Bash` calls in **one message** so they
run concurrently:

```bash
uv run ruff check                                                    # lint
uv run ruff format --check                                           # formatting
uv run ty check -- nextcloud_mcp_server                              # types
uv run pytest tests/unit/ -x --no-header -q                          # unit tests
```

If the diff touches `nextcloud_mcp_server/server/` or `tests/server/`, also run:

```bash
uv run pytest -m smoke -x --no-header -q
```

**If any automated check fails: stop and report.** Don't run the checklist on top of a
broken build — fixing the failures may eliminate findings or change the diff.

### Phase 3 — Read the diff and run the project checklist (2–5min)

```bash
git diff $BASE                                                        # full diff (incl. uncommitted)
git diff $BASE -- '*.py' | head -2000                                # python only, capped
```

Read the **whole diff** before composing findings. Cross-file patterns (test symmetry,
single-source-of-truth, deduplicated logic) are only visible in aggregate.

For each violation, capture:
- **Severity label** (🔴 / 🟡 / 🟢 — see Severity Guide below)
- **Category tag** (A1, B2, J3, etc.)
- **File:line** (new-side line number)
- **One-sentence note** — what's wrong, with a concrete fix direction

### Phase 4 — Output the punch list

Format follows the template in [§ Output Template](#output-template). Group by
severity. Include a brief **Strengths** section to balance the findings — this
matches the tone of the bot review on PRs #733–#747 and avoids the impression that
the review is purely negative.

**Hard rules:**
- Do **not** edit any files.
- Do **not** propose patches inline (one-line fix direction is OK; full code blocks are not).
- Do **not** repeat findings the linter or type checker already surfaces.
- If a finding is style-only and CLAUDE.md doesn't mandate it, demote to 🟢.

---

## Severity Guide

| Label | Meaning | Examples |
|---|---|---|
| 🔴 **blocking** | Must fix before push. Real bug, security issue, or violates an ADR/CLAUDE.md mandate. | Cache-hit bypass on auth gate; raw `List[Dict]` from MCP tool; missing `await` on async call; HMAC compare with strings instead of bytes. |
| 🟡 **important** | Should fix. Not a bug today but a footgun that has caused review-rounds before. | Field description mismatches value; unbounded fan-out without semaphore; `int()` cast inside catch-all `except`; missing `openWorldHint=True`. |
| 🟢 **nit** | Polish. Not blocking, would be nice. | `Optional[X]` when surrounding file uses `X | None`; redundant regex anchors; missing test docstring; pluralization in error messages. |
| 💡 **suggestion** | Alternative approach to consider. No fix expected. | "Could use `frozenset` here for O(1) lookup"; "Could centralise this rewrite in `_make_request`". |
| 🎉 **praise** | Notable good pattern. Worth calling out for visibility. | Cache-hit AND cache-miss paths both enforce the gate; CI-guard test for registry exhaustiveness; explicit `*` keyword-only separator. |

**Escalation rules** (apply per change shape from Phase 1):
- Security-sensitive scope → all J* and K* findings are at least 🟡; auth bypass risks are 🔴.
- MCP surface → D4 (annotations) and I1 (response wrapping) are 🔴.
- Client layer → H1 (duplicate round-trips) is 🟡 minimum.

---

## Project Checklist

Each item is tagged for use in the punch list (e.g. `[B1]`). Items are mined from
PRs #733, #734, #735, #736, #737, #741, #745, #746, #747, #750.

### A. Style & typing (CLAUDE.md)

- **A1** — PEP 604 unions: `X | None`, never `Optional[X]`. (#737, #745, #735)
- **A2** — Lowercase generics: `dict[str, Any]`, `list[T]`, never `Dict`/`List`. (#736, #745)
- **A3** — `anyio` not `asyncio`: `anyio.create_task_group`, `anyio.Lock`, `anyio.run`. Flag any new `asyncio.gather`/`asyncio.Lock`/`asyncio.run`.
- **A4** — Lazy `%`-style logging: `logger.warning("msg %s", var)`, not f-strings, in any file touched on this branch. (memory: feedback_lazy_logging)
- **A5** — Typed signatures: every new/modified function has parameter and return type hints. (#733)
- **A6** — Stay consistent with surrounding file: if a touched file uses legacy `Dict`/`Optional`, flag the introduced inconsistency, not pre-existing debt. (#736, #735, #745)

### B. Error handling specificity

- **B1** — Hoist parsing/casts before network calls: `int()`, `json.loads()`, etc. in their own `try/except (TypeError, ValueError)` *before* the network call. Catch-all `except Exception` over the parse + network produces "unexpected error" instead of a specific log line. (#750-r3, #750-r5)
- **B2** — Narrow exception types: `except RuntimeError`, not bare `except Exception`, when only one failure mode is documented. (#750-r6)
- **B3** — Specific error messages with context: include the problematic value and its surrounding context (e.g. `f"expected int id for {doc_type}, got {type(value).__name__}: {value!r}"`), not opaque re-raises. (#750-r6)
- **B4** — Fail-open vs fail-closed is a deliberate choice: any new fail-open branch (transient error → keep going) needs a log line and a brief comment on *why* fail-open is correct here. (#750-r2)
- **B5** — Except clause order: when catching a subclass and superclass in the same `try`, the subclass must come first. `except TimeoutError` before `except Exception` (TimeoutError is OSError → Exception subtype). (#747)
- **B6** — Defensive parsing envelope: when extracting fields from external payloads, wrap in `try/except (KeyError, TypeError, ValueError)` and return a sentinel. Bubbling `ValueError` from `int("not-a-number")` to the caller is a finding. (#747)

### C. Comment honesty & documentation

- **C1** — Comments must match behavior: don't say "background" if the code runs inline; don't say "evicts asynchronously" if it `await`s. (#750-r1)
- **C2** — Document all return-None / sentinel cases: if a function returns `None` for both 404 *and* malformed XML, the docstring must enumerate both. (#750-r6)
- **C3** — Cross-references point at canonical/earlier definition: "Mirrors `_verify_X`" should reference the function defined first. (#750-r1)
- **C4** — TODOs include WHY: `# TODO(perf): get_items(batch_size=-1) fetches all items at query time` ✓; `# TODO: optimize` ✗. (#750-r3)
- **C5** — Defensive code carries its reason: `.get(key, default)` that "shouldn't fire in practice" needs a comment saying so — otherwise readers treat it as load-bearing. (#750-r6)
- **C6** — Surprising-on-first-read code needs a one-liner: e.g. `password=token` for bearer auth is surprising; a `# caldav reuses the password slot for bearer tokens` line saves readers from suspecting copy-paste bugs. (#734)
- **C7** — Mark deliberate omissions: when a code path *intentionally* skips a guard (e.g. DELETE returns empty body, no shape guard), say so explicitly. Otherwise future maintainers will treat it as an oversight. (#736)

### D. API & type boundaries

- **D1** — Public response models narrow, internal types may widen: if `SearchResult.id: int | str` for forward-compat, `SemanticSearchResult.id: int` and convert at the boundary with an explicit cast that fails loudly. (#750-r3)
- **D2** — Field descriptions match the value: a field described as "unique documents" must hold a unique-document count, not a chunk count. Watch for `len(results)` assigned to a field whose docstring implies dedup. (#750-r1, #735)
- **D3** — Symmetry between related fields: `verified_count` and `dropped_count` should count at the same granularity. (#750-r6)
- **D4** — MCP tool annotations (ADR-017): tools that hit Nextcloud have `openWorldHint=True`. Read-only ops have `readOnlyHint=True`. Destructive ops have `destructiveHint=True` + `idempotentHint=True`. Create ops have `idempotentHint=False`. Update ops have `idempotentHint=False` (etag changes mean different inputs). (#741)
- **D5** — Keyword-only separators: use `*` in signatures with multiple optional params of the same/related types, e.g. `def __init__(self, *, password=None, token=None)` to prevent positional misuse. (#734)
- **D6** — Defaults match documented constraints: if the docstring says "max 1000 characters", add `Field(max_length=1000)` — don't rely on the server returning 400. (#737)
- **D7** — Create/update tools return `*Response` wrappers, not raw models: any new `@mcp.tool` returning a raw `BaseModel` (not `BaseResponse` subclass) is a finding. (#737, CLAUDE.md MCP Response Patterns)
- **D8** — Pagination metadata: a field called `total` should be the server-side total, not page count. Use `count` for "returned in this page" or add `has_more`. (#737)

### E. Concurrency, resource bounds & lifespan

- **E1** — Bound concurrency: any new `asyncio.gather` / task-group fan-out over user data needs an `anyio.Semaphore` cap (default 20 in this repo). (#750-r1)
- **E2** — Bound over-fetching: `limit * K` for filtering must have a fixed `K`. Unbounded N-types or unbounded `K` is a finding. (#750-r1)
- **E3** — Don't snapshot mutable singletons: `eviction_task_group` set during lifespan startup must be read via `@property`/accessor, not snapshotted into another object's `__init__` (order-sensitive race). (#750-r5)
- **E4** — Comment safety of dict/list mutation across tasks: when multiple concurrent tasks write distinct keys to a shared dict, add a one-line comment explaining cooperative-multitasking safety. (#750-r1)
- **E5** — Lifespan context symmetry: when adding fields to one of `AppContext` / `OAuthAppContext`, mirror them in the other to prevent silent regressions in OAuth mode. (#746)
- **E6** — Security gates apply on cache-hit AND cache-miss paths: an allowlist or scope check must run after both branches resolve. A test asserting cache-hit enforcement is mandatory. (#745)

### F. Tests

- **F1** — Symmetry across verifiers/handlers: if you added a 403 test for the notes verifier, add one for files/deck/news too. Asymmetric coverage in a registry-style module is a finding. (#750-r5, #741)
- **F2** — Fail-open paths have a test: any verifier or handler with a fail-open branch (non-numeric id, malformed payload) has a unit test that exercises it. (#750-r5, #750-r6)
- **F3** — CI guard tests for registries: registries (e.g. `INDEXED_DOC_TYPES → verifier`) should have a test that the registry is exhaustive. (#750-r2)
- **F4** — New public functions have tests: any new MCP tool, client method, or Pydantic model without a unit test is a 🟡 finding (or 🟢 if integration-only).
- **F5** — `caplog` is logger-scoped: prefer `caplog.set_level(logging.WARNING, logger="nextcloud_mcp_server.client.notes")` over plain `caplog.at_level("WARNING")` to avoid false positives from other loggers. (#736)
- **F6** — Wire-through tests at the right layer: pure-unit tests on the leaf class are great, but a test that the parent (`NextcloudClient.from_env`) actually threads the param through is a common gap. (#734)
- **F7** — Production-evidence regressions get a unit test: any bug found via CloudWatch / production logs (not in tests) needs a unit test added in the fix PR. (#746)
- **F8** — Test docstring consistency: if 4 of 5 new tests have docstrings, give the 5th one too. (#735)

### G. Configuration, docs & ADRs

- **G1** — New config knobs go through `Settings` + dynaconf validator: any new env var read directly via `os.getenv` in module code (rather than via `Settings`) is a finding. (#750-r5)
- **G2** — Resolve config lazily: `default = settings.X` at function-call time, not at decoration / module-import time, so test overrides work. (#750-r5)
- **G3** — User-facing docs cover non-obvious costs: new code paths with measurable latency (unbounded fetches, per-item round-trips) need a note in `docs/configuration.md` or the relevant ADR. (#750-r2, #750-r6)
- **G4** — ADRs match shipped interface: example code blocks in any modified ADR reflect the actual module's public API. If the ADR shows `Verifier` but the code ships `BatchVerifier`, the ADR is wrong. (#750-r3)
- **G5** — Tool docstrings document hidden round-trips: if an MCP tool now does an extra fetch per result (e.g. post-verification race guard), the docstring says so. (#750-r6)
- **G6** — Migration notes for breaking changes: a PR that silently changes failure modes (e.g. previously-allowed clients now get 401) needs a migration note in the PR description and ideally a startup log line. (#745)
- **G7** — Priority order in env var fallbacks: if a setting reads from `WEBHOOK_INTERNAL_URL` → `NEXTCLOUD_MCP_SERVER_URL` → `/.dockerenv` → localhost, document the order in code comments. (#747)

### H. Performance hygiene

- **H1** — No duplicate round-trips: if data needed by a verifier/handler is already in `SearchResult.metadata` or a passed-in object, don't re-scroll/re-fetch it. (#750-r1)
- **H2** — Single source of truth: hardcoded lists of doc types / scopes / app names in multiple files should reference one constant (e.g. `INDEXED_DOC_TYPES`). (#750-r2)
- **H3** — Limit clamping at the right layer: `min(max(1, limit), 200)` at the client layer gives a clear error rather than a confusing server-side mismatch. (#741)
- **H4** — Avoid empty JSON bodies in HTTP requests: `json=body or None` (instead of `json=body`) prevents sending `{}` and a spurious `Content-Type: application/json` header for bodyless calls. (#741)

### I. MCP response patterns (CLAUDE.md)

- **I1** — No raw `List[Dict]` from MCP tools: tools return Pydantic models inheriting from `BaseResponse`. FastMCP mangles raw lists into dicts with numeric string keys. **🔴 blocking when violated.**
- **I2** — Co-author trailer in commits: each AI-assisted commit ends with `Co-Authored-By: Claude ...`. Missing trailer on AI-touched commits is a 🟢 note.

### J. Security & input validation

- **J1** — Validate untrusted identifiers before HTTP layer: room tokens, paths, IDs that go into URLs must be validated against an allowlist regex (e.g. `r"[A-Za-z0-9]+"` with `fullmatch`) *before* the request is built — not after. Path traversal (`../etc/passwd`) is the canonical example. (#741)
- **J2** — `hmac.compare_digest` on bytes: both sides encoded to `bytes` before comparison. Comparing strings is incorrect. (#747)
- **J3** — HTML-escape user-controlled values in HTML responses: even when the input is gated by an upstream check (e.g. `get_preset(preset_id)` returning `None` for unknown), escape on the success path too. Defense-in-depth, not single-layer. (#747)
- **J4** — Static-config-but-still-rendered fields should be escaped consistently: if dynamic fields are escaped and static fields aren't, the inconsistency itself is the smell. (#747)
- **J5** — Privacy defaults: defaults for polling/read-receipt parameters should minimize side effects (e.g. `no_status_update=True` for chat polling so users don't appear "online" on every poll). (#741)
- **J6** — Server-layer hardening even when client supports it: if an MCP tool calls a client with hardcoded safe values (e.g. `look_into_future=False`), don't expose the unsafe option as a tool parameter. (#741)
- **J7** — Redundant regex anchors with `fullmatch()`: `re.compile(r"^[A-Za-z0-9]+$").fullmatch(...)` — the `^`/`$` are redundant and misleading. Use `re.compile(r"[A-Za-z0-9]+")` with `fullmatch`. (#741)

### K. Trust boundaries

- **K1** — Comment where input comes from at trust boundaries: webhook payloads, request body fields, query params — say "user-controlled" or "static config" so future readers know the threat model. (#747)
- **K2** — Differentiate HTTP status codes by retry semantics: 503 for "try again" (queue saturated, dependency down), 500 for "broken" (genuine bug). 401 without `WWW-Authenticate` is correct when the caller has no auth state machine (e.g. Nextcloud webhook delivery). (#747)
- **K3** — Trust-boundary comments at API entrypoints: when a `uri` field in a request body is taken at face value (e.g. Astrolabe provides its own webhook URI), document the trust assumption. (#747)

### L. Sentinel values & PHP API quirks

- **L1** — `0` as a valid value vs `None` as "absent": fields like `lastReadMessage` or `expirationTimestamp` may use `0` for "no messages read" or "never expires". Don't truthy-test them. Document semantics in field docstrings. (#741)
- **L2** — PHP empty-array-as-list quirks: spreed and other PHP backends serialize empty objects as `[]`. Pydantic validators must coerce `[] → {}` for object-shaped fields and `[] → None` for nullable single-object fields. (#741)
- **L3** — `isinstance(value, (date, datetime))` is redundant: `datetime` is a subclass of `date`. Either drop `datetime` from the tuple, or distinguish behavior between the two. (#735)
- **L4** — Field description matches serialized form: a field documented as "ISO date format" but accepting `datetime` will serialize as `"2000-01-01T12:30:00"`, not `"2000-01-01"`. Either coerce to date or update the description. (#735)

### M. Defensive code patterns & symmetry

- **M1** — `getattr(obj, "field", None)` masks future typos: when an attribute is *expected* to exist post-fix, prefer direct access so a typo (`document_recieve_stream`) fails loudly. The defensive `getattr` is the right call only when the attribute is genuinely optional. (#746)
- **M2** — Both-credentials / multiple-mode case has explicit precedence: when two mutually-exclusive params can both be set, either raise `ValueError` or `logger.warning` — silent precedence is a footgun. (#734)
- **M3** — Defensive dict construction over passing `None`: `kwargs = {"password": password, "auth_type": "basic"} if password else {"auth_type": "bearer"}` — don't pass `None` into constructors that don't accept it. (#734)
- **M4** — Speculative defensive code without observed cause: if you add an unwrap for `[{...}] → {...}` "in case the API returns it", cite the version/route where it's been observed. Otherwise it silently masks contract changes. (#736)
- **M5** — Idempotency of URL/path transforms: `_resolve_url` should be safe to call twice. If it adds `/index.php` only when missing, document and test the idempotency. (#733)

---

## What NOT to Flag

- **Pre-existing technical debt** outside the diff. Note as 💡 if relevant, but don't list as a blocking finding.
- **Style violations the linter already catches.** ruff and ty are authoritative for what they cover. Manual style checklist is for things they don't cover (PEP 604 unions, lazy logging, `Optional` vs `|`).
- **Code formatting** — `ruff format --check` is the source of truth.
- **Bikeshed-tier preferences** (variable naming taste, line breaks, comment phrasing).
- **Anything you'd phrase as "I would have done it differently"** without a concrete repo-pattern violation.
- **Findings that duplicate the bot's previous review** if there's an open PR with one already (use `gh pr view --comments` to check).

If a category has zero applicable findings, omit it from the punch list. Don't pad.

---

## Output Template

```
# Pre-push review for <branch>

**Base:** <base> · **Commits:** <N> · **Files changed:** <M>
**Change shape:** <classification from Phase 1>

## Automated checks
- ruff: ✅ / ❌
- format: ✅ / ❌
- ty: ✅ / ❌
- unit tests: ✅ / ❌ (X passed, Y failed)
- smoke (if run): ✅ / ❌

[If any check failed, stop here and report. Otherwise continue.]

## Findings

### 🔴 Blocking (<count>)

1. **[J1]** `nextcloud_mcp_server/client/talk.py:42` — Token interpolated into
   URL before validation; path-traversal risk. Move `_validate_token(token)`
   above the `httpx.URL(...)` call.

### 🟡 Important (<count>)

1. **[D2]** `nextcloud_mcp_server/models/semantic.py:87` — `verified_count`
   description says "unique documents" but value is `len(verified_results)`
   (chunk count). Either reword or dedup the value.

2. **[F1]** `tests/unit/search/test_verification.py` — 403 tests exist for
   notes/deck verifiers but not for files/news. Add for symmetry.

### 🟢 Nits (<count>)

1. **[A1]** `nextcloud_mcp_server/server/deck.py:118` — `Optional[int]` →
   `int | None` per CLAUDE.md.

### 💡 Suggestions

1. `nextcloud_mcp_server/auth/client_registry.py` — Pre-existing legacy
   `Dict`/`Optional` typing throughout. If touching anyway, opportunity
   to modernize.

## Strengths

- 🎉 **[E6]** Allowlist enforced on both cache-hit and cache-miss paths in
  `verify_token_for_management_api`, with a dedicated test
  (`test_cache_hit_also_enforces_allowlist`). Exemplary security gate.
- 🎉 **[F3]** New `test_supported_doc_types_covers_indexed_types` enforces
  registry exhaustiveness — will catch any future doc_type added without a
  verifier.

## Notes

- 5 commits on branch; all have `Co-Authored-By` trailer ✓
- ADR-019 implementation checklist all marked [x] (G4 ✓)
```

### Clean diff (no findings)

```
# Pre-push review for fix/notes-etag-handling

**Base:** master · **Commits:** 2 · **Files changed:** 3
**Change shape:** client layer

## Automated checks
- ruff: ✅
- format: ✅
- ty: ✅
- unit tests: ✅ (412 passed)

## Findings

No checklist violations found.

## Strengths

- 🎉 **[B1]** ETag parsing hoisted into its own try/except before the
  network call — error logs will be specific to malformed ETag, not
  generic "unexpected error".

Safe to push.
```

---

## Notes for the Running Model

- Read the **whole diff** before composing the report. Cross-file patterns
  (F1 symmetry, H2 single-source-of-truth, E5 lifespan symmetry) are only
  visible in aggregate.
- Keep findings **specific and short**. One sentence each plus a fix direction.
  The point is to feed them back into the main loop for fixing, not to write
  a treatise.
- If an automated check fails, **stop and report** — don't run the checklist
  on top of a broken build.
- The checklist is calibrated to *this repo's* recurring review feedback. If
  the diff is in an unrelated area (CI config, random docs), most items won't
  apply — say so rather than padding.
- **Don't fix anything.** This skill reports; the main loop fixes. Even if a
  finding is a one-character change, leave it for the user to action.
- Use `gh pr view <PR#> --comments` (not `gh api`) when checking for prior bot
  reviews on an existing PR — see memory feedback_use_gh_pr.
