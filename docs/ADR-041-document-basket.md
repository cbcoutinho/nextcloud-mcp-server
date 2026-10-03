# ADR-041: Document baskets and processors (generalising SAR cases)

## Status

Proposed — 2026-10-03. Supersedes the *case model* and *surfaces* sections of
[ADR-040](ADR-040-sar-redacted-export.md). ADR-040's archive format and
redaction rules are unchanged; they become the `sar_redact` processor.

## Context

ADR-040 built a SAR **case**: a durable, shared object to which a user adds
documents from one or more searches, then exports once as a redacted archive.
Only the last step is SAR-specific. The same pattern is wanted for other jobs:

- assign a system tag to every collected document;
- copy or move the collected files into a folder;
- later, run a Nextcloud AI task over the files.

Today each of these would need its own "case". This ADR splits the case into a
generic **basket** (collect) and a pluggable **processor** (act once on the
whole basket).

## Decision

### Mapping from SAR cases

| ADR-040 (SAR case) | Basket |
|---|---|
| `sar-case.json` in the case folder | `basket.json` in the basket folder (legacy `sar-case.json` still read) |
| case id = file id of `sar-case.json` | basket id = file id of the basket file (unchanged rule) |
| `open → exporting → ready_for_audit → closed` | `open → processing → done → closed` (reopen from `done`; failed run → `open`) |
| items `{doc_type, doc_id, title, reason, found_by, page_start, page_end}` | same fields; `reason` is an optional note on any basket |
| query log (`sar_case_search`, full filters, de-duplicated) | unchanged, generic search provenance |
| subject keep-list | `sar_redact` processor's **basket data**, set at creation |
| `exports[]` (versioned archives, progress) | `runs[]`: `{n, processor, options, state, processed, failed, skipped, result}` |
| `sar_available` | `basket_processors` (list) — `sar_available` kept while `sar/*` is served |

Storage, ETag-guarded read-modify-write, access-by-folder-permission and the
limits (2,000 items, 1,000 logged searches) are unchanged from ADR-040.

### Basket model

```
open ──run(processor)──▶ processing ──ok──▶ done ──close──▶ closed
  ▲                          │ failed          │
  └──────────────────────────┴──── reopen ─────┘
```

- **One run at a time.** The basket locks (`processing`) while a run is
  active, so a run always sees a fixed item set. A second run, and any change
  to items or basket data, is a 409 until the run ends.
- **Progress lives in the run's status file, not the basket file.** As in
  ADR-040, each run writes `runs/<n>.status.json` next to the basket: counts,
  per-item outcomes and a heartbeat. It has one writer (the run), so it needs
  no ETag guard. There is exactly **one writer task** per run: the processor's
  `report.item()` calls only buffer in memory, and a single flusher task writes
  the whole snapshot. It flushes when the buffer is dirty (at most once every
  5 s), at least every 60 s even when nothing changed (that is the heartbeat),
  and once when the run ends. Because counts and heartbeat come from one
  snapshot, a heartbeat can never overwrite fresher counts. The number of
  status writes is bounded by the run's duration, not its item count. The
  status file is **created at lock time**, in the same step that locks the
  basket, so a run killed before its first flush still has a file whose age
  can be judged. The basket file itself is written twice per run: lock at
  the start, unlock with the run summary at the end.
- **A dead run cannot lock a basket.**
  - *Heartbeat is the framework's job.* The flusher above runs in the run's
    task group for the run's lifetime, independent of the processor, so a
    processor blocked on one slow item (an OCR'd PDF, an `nc_task` poll)
    cannot forget to heartbeat.
  - *Staleness is judged on Nextcloud's clock.* The heartbeat's age is the
    status file's `getlastmodified`, which Nextcloud sets, compared with the
    `Date` header of the same WebDAV response. Replica clocks never enter it.
    A run is stale past `BASKET_RUN_STALE_SECONDS` (default 900).
  - *Reads compute, writes persist.* `basket_get` and `basket_list` (which
    already read each basket's latest run status) show a stale run as `failed`
    (`interrupted`) and the basket as `open`, without writing. That works for a
    caller with only `baskets.read` or a read-only share. The recovery is
    persisted by the next write-capable operation on that basket (any
    `baskets.write` call, including a new run), through the usual ETag-guarded
    write, so two replicas resolving it at once agree.
  - *A run that was declared dead stays dead.* The basket's `runs[n].state` is
    the source of truth, not the status file. A slow-but-alive run (for
    example, one that resumes after a network partition) ends with a
    *conditional* unlock: inside the ETag-guarded mutation it checks that the
    basket is still `processing` and that the active run is still its own `n`.
    If not, because it was recovered and maybe re-run meanwhile, it changes
    nothing in the basket. It logs the outcome and leaves its own status file
    as the record of what it did. The ETag guard alone would not give this:
    it only detects concurrent writes, not a basket that has moved on. Side
    effects the late run already made (tags assigned, files copied or moved)
    stay recorded per item in that status file.
  - This also closes a gap ADR-040 left open: today a killed process (as
    opposed to a cancelled task) leaves a case in `exporting` for good.
- **Closing is final**, as in ADR-040. A `closed` basket is read-only and
  cannot be reopened. Only `done` (and a failed run, which returns to `open`)
  leads back to `open`.
- **Runs are history.** Every run appends to `runs[]` with its processor id,
  options, counts and result (archive path, target folder, tag ids). Reopening
  and running again appends a new run; SAR archive versions (`-v1`, `-v2`) are
  the `sar_redact` runs numbered per basket. Per-item outcomes stay in the
  run's status file, so `runs[]` holds only summaries. It is capped at 100
  runs per basket: a run beyond that is a 409. Status files share the basket
  folder's lifecycle. Closing keeps them as the audit record, deleting the
  basket folder deletes them, and nothing reaps them earlier.
- **Item results.** A run reports, per item, `ok`, `failed` (with reason) or
  `skipped` (with reason). Nothing is dropped silently; ADR-040's "listed as
  failed, never dropped" becomes a rule for every processor.

### When is the processor chosen? Both, by processor

A processor declares `binds_at`:

- **`create`** — needs data on the basket before collection starts. A basket
  bound to it carries `kind: <processor id>` plus that processor's basket data,
  validated against the processor's `basket_schema`. `sar_redact` is the only
  one: the subject keep-list is entered up front and shown while collecting.
- **`run`** — chosen when the user runs the basket, with options validated
  against `options_schema`. `tag`, `copy`, `move` and `nc_task` are run-time.

**`kind` is immutable** once the basket exists (a `PATCH` that sets it is a
400), and a kind is exactly one processor id. Basket data is validated against
the processor's `basket_schema` on every update, not only on create. This is intentional: a future create-time processor gets its own
kind rather than sharing a family. Basket data (the keep-list)
stays editable while the basket is `open`, as the subject is today, and is
frozen while `processing`. It is applied only when the processor runs: the
query log stores searches as typed, and `sar_redact` redacts the log with the
keep-list current at run time, exactly as ADR-040's `searches.pdf` does. So an
edit mid-collection never leaves an earlier log entry redacted against an old
subject.

Any basket can be run with any `run`-time processor whose `item_types` match at
least one item. A `create`-time processor can only run on a basket of its own
`kind`. So a SAR basket can also be tagged or copied, but a plain basket cannot
be SAR-exported without first being given a subject — which is deliberate: the
keep-list decides what is redacted.

### Processor registry

In-process, in the MCP server, a plain mapping of id → processor:

```python
class Processor(Protocol):
    id: str                      # "sar_redact", "tag", "copy", "move", "nc_task"
    title: str
    binds_at: Literal["create", "run"]
    item_types: frozenset[str]   # {"file"}, or {"*"} for any doc_type
    basket_schema: dict | None   # JSON Schema, binds_at == "create" only
    options_schema: dict         # JSON Schema for the run's options
    scopes: tuple[str, ...]      # extra token scopes the run needs
    destructive: bool            # move
    def available(self, settings: Settings) -> bool: ...
    async def run(
        self, nc: NextcloudClient, basket: Basket, options: dict, report: RunReport
    ) -> RunResult: ...


class RunReport(Protocol):
    async def item(self, item: BasketItem,
                   outcome: Literal["ok", "failed", "skipped"],
                   detail: str = "", **result: str) -> None: ...
    # buffered into the run status file; `result` carries per-item facts
    # such as copy/move's `from_path` and `to_path`
```

Cancellation belongs to the task group: a run is cancelled only by shutdown,
and the shielded unlock from ADR-040 records it as `failed`. There is no
user-facing "cancel run" in this ADR.

Processors are code, not configuration: there is no plugin loading. A new
processor is a new module and a registry entry.

| Processor | Items | Options | Backend | Available when |
|---|---|---|---|---|
| `sar_redact` | `*` (notes, files, …: index text; an item not in the index is `failed`, as in ADR-040) | output folder | ADR-040 export (index text + `/v1/ner`) | `sar_available(settings)` |
| `tag` | `file` | `tags: [name]`, `create_missing: bool` | `WebDAVClient.get_or_create_tag` + `assign_tag_to_file` | always |
| `copy` | `file` | `target`, `on_conflict: skip\|rename` | `WebDAVClient.copy_resource` | always |
| `move` | `file` | `target`, `on_conflict: skip\|rename` | `WebDAVClient.move_resource` | always |
| `nc_task` (optional) | `file` | `task_type`, task input | TaskProcessing OCS API | `BASKET_TASKPROCESSING_ENABLED` |

Non-file items reaching a file-only processor are **skipped and reported**
(`skipped: not a file`). For `copy` and `move`:

- **Paths are resolved by file id at run time.** Items store file ids, which
  survive a move, so a basket whose files were moved (by a previous `move` run
  or by hand) still resolves. A file already in the target folder is
  `skipped: already in target`, which makes re-running a partly failed `move`
  safe: it finishes the remainder instead of failing everything.
- **Every item records `from_path` and `to_path`** in the run result, so a
  `move` that stops partway is auditable and can be put back by hand.
- **Nothing is overwritten**: the default conflict policy is `skip`.
- **The `confirm` rule.** `move` is the only destructive processor. Its
  `options_schema` requires `confirm: true` (`"const": true`), and the schema
  is validated wherever `move` runs: the HTTP `/runs` route and the
  `basket_move` tool. A `move` without it is a 400. `basket_run` does not
  accept `processor: "move"` at all (also 400). So there is exactly one way to
  run `move` per surface, and each requires `confirm`. The duplication with
  `basket_move`'s destructive hint is deliberate: the hint is advisory for the
  client, while `confirm` is enforced by the server.

### API

The ADR-040 operations, renamed, plus one run endpoint:

| Operation | MCP tool | HTTP |
|---|---|---|
| Create (optional `kind` + basket data) | `basket_create` | `POST /api/v1/baskets` → 201 |
| List | `basket_list` | `GET /api/v1/baskets` |
| Get (items paged, runs) | `basket_get` | `GET /api/v1/baskets/{id}` |
| Name, description, basket data, close, reopen | `basket_update` | `PATCH /api/v1/baskets/{id}` |
| Add/update/remove items, log queries | `basket_items` | `POST /api/v1/baskets/{id}/items` |
| Search for the basket, logged with its filters | `basket_search` | `POST /api/v1/baskets/{id}/search` |
| Run a non-destructive processor | `basket_run` | `POST /api/v1/baskets/{id}/runs` `{processor, options}` → 202 |
| Run `move` | `basket_move` | the same route, `processor: "move"` |
| Processor titles and schemas | `basket_processors` | `GET /api/v1/baskets/processors` |

Scopes: `baskets.read` (list, get, processors) and `baskets.write`
(everything else), plus each processor's own `scopes` on a run (`sar_redact`:
`sar.write` and `semantic.read`, as today; `tag`/`copy`/`move`: `files.write`).
`@require_scopes` is static per tool, so on `basket_run` it carries only
`baskets.write`. The per-processor scopes are checked in the body once the
processor is resolved: `check_scopes(ctx, *processor.scopes)` for the MCP
tools, and `_authorize(request, "baskets.write", *processor.scopes)` for the
HTTP route. As in ADR-040, a basket scope never widens what can be read: every
item is access-checked as the calling user.

**A run request is checked in a fixed order**, so each error is testable and
none leaves a lock behind:

1. 403: no `baskets.write`.
2. 404: no such basket, or no access to it.
3. 400: unknown or unavailable processor; `create`-time processor on a basket
   of another kind; an empty basket, or one with no item the processor's
   `item_types` accept (for every processor, ADR-040's "at least one document"
   rule); options failing the schema (including `move` without `confirm`); or
   `move` sent to `basket_run`.
4. 403: missing processor scope.
5. 409: basket not `open`.
6. Lock, then 202.

Checking the processor (3) before its scopes (4) reveals nothing beyond
`/api/v1/status`, which already lists the available processor ids. That list
does hint at server configuration (whether NER or TaskProcessing is set up),
as `sar_available` does today.

**Annotations (ADR-017).** `move` is its own MCP tool so the hints are exact.
`basket_run` is `destructive_hint=False` (`tag` and `copy` only add, and
`sar_redact` writes a new archive). `basket_move` is `destructive_hint=True`.
Both are `idempotent_hint=False`, because each call appends a run. The other basket tools follow
ADR-017 as for ADR-040's SAR tools.

### Advertising

`GET /api/v1/status` is public, so it gains only a summary, always present
(an empty list when off):

```json
"basket_processors": [
  {"id": "sar_redact", "binds_at": "create", "item_types": ["*"], "destructive": false},
  {"id": "tag", "binds_at": "run", "item_types": ["file"], "destructive": false}
]
```

That is enough to gate the UI, and it reveals no more than `sar_available`
does today. Titles and the `basket_schema`/`options_schema` JSON Schemas are
served by the authenticated `GET /api/v1/baskets/processors` (`baskets.read`).
Only processors whose `available(settings)` is true are listed in either
place. The basket routes themselves are served when the list is non-empty.
Because `tag`, `copy` and `move` are always available, in practice that means
every authenticated deployment mode.

Astrolabe extends `SearchCapabilities` with `getBasketProcessors()` (cached like
`isSarAvailable()`), and:

- shows "Add to basket" only while a basket is open (the existing
  `sarCollecting` pattern);
- builds the processor picker from the list, rendering option forms from
  `options_schema`, with the SAR panel as the `sar_redact` basket form;
- hides any processor the server did not list.

### Compatibility

Decision: **keep `sar/*` as an alias for one minor release, then remove it with
a `BREAKING CHANGE:` footer.**

- **Release N** (`feat:`, introduces baskets): `/api/v1/sar/cases/*` and the
  `sar_case_*` tools become thin adapters over baskets. They list only baskets
  of kind `sar_redact`, map the states back (`processing` → `exporting`, `done`
  → `ready_for_audit`) and expose `sar_redact` runs as `exports`. Every alias
  response carries `Deprecation: @<unix-ts>` (RFC 9745, the time release N
  was cut) and a `Link` to ADR-041 with `rel="deprecation"`, plus a
  deprecation warning in the logs once per process. There is no `Sunset` date,
  because removal is keyed to the next minor release, not a calendar date. The
  adapters keep the SAR scopes: every alias call still needs `sar.read` or
  `sar.write`, plus `semantic.read` for search and export, exactly as in
  ADR-040. They never fall back to `baskets.*` alone.
  `sar_available` is still
  advertised and keeps its meaning. The alias keeps the item contract as of
  v0.198.4 (#1595): `reason` is optional on every basket, `sar_redact` ones
  included.
- **Release N+1** (`feat!:` with `BREAKING CHANGE: /api/v1/sar/cases and the
  sar_case_* tools removed; use /api/v1/baskets with processor sar_redact`):
  the alias is removed **and `sar_available` is advertised as `false`**. That
  last part is what keeps an older Astrolabe safe: it reads `sar_available`,
  hides its SAR UI, and never calls the removed routes. It degrades to "no SAR"
  rather than a broken page.
- **Astrolabe decides from one field.** When `basket_processors` is
  **present**, it is authoritative: SAR is shown if and only if `sar_redact` is
  in the list, and `sar/*` is never called. A server that lists baskets without
  `sar_redact` (NER unconfigured, say) also reports `sar_available: false`, but
  Astrolabe does not rely on that. Only when the key is **absent** (a server
  older than baskets) does it fall back to `sar_available` and `sar/*`. So an
  older server (no baskets) keeps the full SAR UI, and a release-N server works
  with either Astrolabe. Astrolabe never assumes a server version from its own.
- Existing case folders need no migration: the basket loader reads
  `sar-case.json` as a basket of kind `sar_redact` and maps the fields above.
  New writes to such a basket keep the legacy file name, so the basket id (its
  file id) never changes. `basket.json` is used only when a basket is created,
  in a new folder, so a folder never holds both files. Creating a basket in a
  folder that already has either file is a 409.

The PR descriptions state the versions ("baskets added in 0.N.0; `sar/*`
removed in 0.N+1.0"), never a merge order.

### Nextcloud Flow and TaskProcessing

**Flow can feed a basket, not be one.** `OCP\WorkflowEngine` operations
(`IOperation::onEvent`, `ISpecificOperation`, `RegisterOperationsEvent`, all
`@since 18.0.0`) fire **per file event**: there is no set and no "run once over
these" trigger, so neither the basket nor the run can live in Flow.

- **First, and no Flow at all: "Add files with tag …"**, a one-shot basket
  action in the MCP server using the existing `WebDAVClient.get_files_by_tag`.
  It runs as the calling user in a normal request, so it needs no new auth,
  and it covers most "fill from a tag" needs.
- **Deferred: an Astrolabe Flow operation "Add to basket"** (an
  `ISpecificOperation` on the File entity, user-scope only, fired by a
  tag-assigned rule, `MapperEvent::EVENT_ASSIGN`, `@since 9.0.0`). It has an
  unsolved auth problem. Flow operations run from event dispatch, often a
  background job with no session. Astrolabe stores no MCP tokens:
  `McpTokenMinter::mintForUser()` mints one per request. With the internal
  `oidc` IdP that may work without a session. With an external IdP it
  exchanges the user's own `user_oidc` login token, which a background job
  does not have. The operation therefore cannot reliably call `POST …/items` as
  the flow owner in every deployment. It needs its own design: for example, a
  server-side "add by file id" that uses the user's stored app password, as
  background ingest already does. That design must also say which scope such a
  call holds. Until then, this operation is out of scope.

**TaskProcessing is an optional processor backend, `nc_task`.** The MCP server
is Python, so it uses the OCS API, not OCP: `GET /ocs/v2.php/taskprocessing/tasktypes`
to find task types with a `ListOfFiles` input slot (`EShapeType::ListOfFiles`,
`@since 30.0.0`), `POST …/taskprocessing/schedule` with the basket's file ids
and `customId: basket:<id>:run:<n>` (a correlation hint for logs and the task
list only, never trusted; the run already holds the task id), then
`GET …/taskprocessing/task/{id}` to poll. The task is scheduled with the
calling user's credentials, so it is that user's task. Nextcloud checks every
file id in a `ListOfFiles` input against that user when scheduling
(`Manager::validateUserAccessToFile`, also on stable32), so a basket still never
widens access. One basket run is one task: progress is the task's single float,
and per-item results are only "submitted". It does **not** replace `sar_redact`:
SAR needs archive-wide placeholder numbering, the subject keep-list, and
per-document failure, none of which a generic task gives. No Astrolabe PHP is
needed for `nc_task`; a provider-side integration
(`GetTaskProcessingProvidersEvent`, `@since 32.0.0`) is out of scope.

**Files app**: an "Add to basket" batch action (`@nextcloud/files`
`registerFileAction` with `execBatch`) in Astrolabe lets users fill a basket
from the file list. Optional; the API is the same `POST …/items`.

### Minimum Nextcloud version

**Stays at 32. No bump.** Every API this design touches, checked by `@since`
in `nextcloud/server` and confirmed present on its `stable32` branch:

| API | `@since` |
|---|---|
| `OCP\WorkflowEngine\IOperation::onEvent`, `ISpecificOperation`, `Events\RegisterOperationsEvent` | 18.0.0 |
| `OCP\SystemTag\ISystemTagObjectMapper`, `MapperEvent::EVENT_ASSIGN` | 9.0.0 |
| `OCP\TaskProcessing\EShapeType::ListOfFiles`, `Task`, `TaskSuccessfulEvent`/`TaskFailedEvent` | 30.0.0 |
| TaskProcessing OCS `tasktypes` / `schedule` / `task/{id}` | 30 (present on stable32) |
| `Task::setAllowCleanup`, `Events\GetTaskProcessingProvidersEvent` (not used, listed for completeness) | 32.0.0 |
| WebDAV COPY/MOVE, `systemtags` / `systemtags-relations` | long-standing |

**Not used, because they are 33+ and absent from stable32:**
`OCP\TaskProcessing\ITriggerableProvider` and `OCP\TaskProcessing\IInternalTaskType`
(both `@since 33.0.0`). Any further OCP API picked during implementation must be
checked the same way; one that is 33+ forces either a fallback path or an
explicit minimum-version bump in Astrolabe's `appinfo/info.xml`, recorded here.

## Test plan

Every new API surface ships with e2e and contract coverage in the same PR.

- **MCP server, unit**: registry (availability, schema validation, `binds_at`
  rules, the 400 for a `kind` mismatch, `move` without `confirm`), state
  machine (one run at a time, failed run reopens, a stale heartbeat unlocks the
  basket), the per-processor scope check (403, basket left `open`), legacy
  `sar-case.json` loading, alias state mapping, each processor with a fake
  WebDAV client (`tag`/`copy`/`move` skip non-files, `on_conflict`, a re-run
  `move` skips files already in the target), the error order for a run
  request, `basket_move` without `confirm` over MCP as well as HTTP, and
  `basket_run` with `processor: "move"` (400, so the split cannot be bypassed
  by calling the wrong tool). Also: a `PATCH` setting `kind` (400), and an
  empty basket (400).
  Stale runs: a reader with only `baskets.read` sees the computed `open` view
  and nothing is written. The next `baskets.write` call persists it. A slow
  item past the stale window stays alive on the framework heartbeat.
- **MCP server, integration** (login-flow lane, real Nextcloud): create basket →
  `basket_search` → add → run `tag` (tags visible via WebDAV), `copy`
  (files in target), `move`; run `sar_redact` (archive written) through both
  `/baskets` and the `sar/*` alias. The alias still refuses a token holding
  only `baskets.*` (403 without `sar.*` / `semantic.read`) and sends the
  `Deprecation` header. Negative case: a basket shared with a user
  who cannot read one of its files; their run reports that item `failed`
  rather than reading it, because a basket scope never widens access.
  Heartbeat end to end: a `sar_redact` run over a document slow enough to
  outlast the stale window (with `BASKET_RUN_STALE_SECONDS` lowered for the
  test) is never shown as stale. Concurrent recovery: two concurrent writers
  against one stale basket (as two replicas would be) persist the recovery
  exactly once, and a late unlock from the dead run leaves the reopened basket
  untouched.
- **Pact provider** (`test_mcp_provider_verification.py`): provider states for
  "a basket exists", "a basket of kind sar_redact exists", "a run is in
  progress"; verifies `/api/v1/baskets/*`, `/runs`, `/baskets/processors`, and
  the `basket_processors` summary in `/api/v1/status`. **Known gap:** only the
  `/api/v1/status` part can be verified today. The basket routes are
  authenticated, and like the existing SAR states (`_state_sar_case`) their
  states stay empty until the ADR-029 phase-4 hook (bearer token plus
  seeded data) exists. That gap is tracked on board 11 card #1340 (SAR e2e +
  authenticated provider verification), which this ADR extends to the basket
  routes. The consumer pacts are still recorded and published in the meantime.
- **Pact consumer** (Astrolabe `McpServerClientPactTest.php`): basket CRUD,
  items, search, runs, and the status shape with and without
  `basket_processors` (the fallback to `sar_available`).
- **Playwright** (Astrolabe `tests/e2e`): collect from two searches → run
  `tag`; SAR flow via `sar_redact`; and the existing `sar.spec.ts` against a
  server advertising only `sar_available`, proving the old path still works.

## Suggested PR stack

1. **nextcloud-mcp-server**: basket model, API, MCP tools, processor registry,
   `sar_redact` as the first processor, `sar/*` alias, `basket_processors` in
   status, stale-run recovery. Unit + integration + **provider** pact (states
   for the new routes).
2. **astrolabe**: basket UI, processor picker from `basket_processors`, SAR
   panel as the `sar_redact` form, fallback to `sar/*`. **Consumer** pact for
   every route it calls + Playwright.
3. `tag`, `copy`, `move` and the "add files with tag" action:
   **nextcloud-mcp-server** (processors + provider states) then **astrolabe**
   (option forms + consumer pact for `/baskets/processors` + Playwright).
4. **Optional**: Files batch action, `nc_task` processor. (The Flow operation
   waits on its own auth design; see above.)
5. **nextcloud-mcp-server**, one release after 1: remove the `sar/*` alias
   (`BREAKING CHANGE:`, `sar_available` → false).

## Consequences

- One collect-then-act workflow serves SAR and the general cases; new
  processors cost a module, not a new object model.
- The alias doubles the SAR surface for one release, and the old routes become
  an adapter that must stay faithful (covered by the alias integration test).
- `move` makes a basket run destructive. It is mitigated by `on_conflict:
  skip`, the required `confirm`, a separate `basket_move` tool, and per-item
  `from_path`/`to_path`. But nothing puts a moved file back automatically.
- **Regression for old Astrolabe on a release-N+1 server.** An Astrolabe that
  predates baskets reads only `sar_available`, which N+1 reports as `false`, so
  it hides SAR entirely even though the server can serve it through
  `sar_redact`. This is the accepted cost of removing the alias: degraded, not
  broken, until Astrolabe is updated. It is stated in the N+1 CHANGELOG entry.
- `nc_task` progress is coarse: one float for the whole basket.
- A basket remains a JSON file in Nextcloud; the ADR-040 limits (2,000 items)
  and the "database table + streaming" upgrade path still apply.
