# ADR-043: MCP Apps — what "general" means for views, hosts and the contract

## Status

Proposed — 2026-11-26. **Validation phase**: this ADR fixes the vocabulary,
the rules a view and a host must follow, and the contract tests, but leaves the
shape of the shared code open until a second view has been built against it
(§9). Breaking changes to the viewer tools and the Astrolabe host are allowed
during this phase. They are version-gated as usual, never merge-ordered (see
*Breaking changes* in `CLAUDE.md`).

Tracked on Deck board 12, card #1612. Reference key: `#N` is a Deck card
(never a GitHub issue), `PR #N` is a GitHub pull request in this repo, and
`note N` is a Nextcloud Notes id in the maintainers' knowledge base. Builds on
the POC (#1525) and the document-first layout (#1604). Contract-test work goes
on board 11, next to the viewer test-gap cards #564 and #565.

## Context

### What exists

One MCP App, the **document viewer**, built on the `io.modelcontextprotocol/ui`
extension (apps spec 2026-01-26) via the python-sdk's `mcp.server.apps.Apps`:

| Piece | Where | Role |
|---|---|---|
| View | `ui://nextcloud-mcp/document-viewer.html` (`server/document_viewer.html`) | ~1,000 lines of HTML/JS, pdf.js and its wasm inlined at serve time (~2 MB) |
| Launch tool | `open_document` (model-visible) | Returns chunk geometry, staged changes, etag |
| App tools | `read_document_bytes`, `detect_personal_data`, `preview_changes`, `apply_changes` (`visibility=["app"]`) | Called only by the view, through the host |
| Scopes metadata | `_meta["nextcloud-mcp/scopes"]` on each tool and the view | Lets a host proxy mint a per-call token |
| Registration | `_app_extensions()` in `app.py` | Only when vector sync is enabled |

Astrolabe is a host in two places: the Astrolabe page and the Nextcloud
Assistant chat (via a reference widget). The host lives in
`lib/Controller/McpAppController.php` (view route, tools/call proxy, write
confirmation), `lib/Service/McpApp/ViewTools.php` (reads `tools/list`
metadata) and `src/components/McpAppHost.vue` (postMessage bridge).

### Hosts the POC has run in

| Behaviour | Astrolabe / Assistant | claude.ai | Le Chat |
|---|---|---|---|
| MCP Apps documented | n/a (our host) | Yes (claude.com connector docs) | **No** for the chat UI; Mistral documents `ui://` only for Studio workflows (`allow_mcp_ui`). It worked in our tenant demo anyway |
| Honours `_meta.ui.csp.resourceDomains` | Yes (builds the CSP from it) | **Documented yes, observed blocked**: cdnjs was refused although the content item carried `resourceDomains` (PR #1913); cause unconfirmed | Not checked |
| `ui/message` | Side-panel agent, no history | User allows it or sends it from a pre-filled composer; 8,000-character limit | Lands in the composer |
| Confirms app-tool writes | Yes: 409 until the user confirms | **No** confirmation observed; none documented | Not checked |
| Elicitation | No | Not documented | Not documented |
| Inline width | Host-chosen | ~355 px inline, ~570 px expanded | Expand failed on token expiry (#1602) |
| Hides app-only tools from the model | Yes (AgentLoop filters) | Yes | Yes |

These differences, not the spec, decide what a view can rely on. The apps
spec (2026-01-26, the only dated version; a `draft/` exists) permits most of
them:

- **CSP:** a host MUST build the CSP from the declared domains, but MAY
  restrict it further.
- **App-tool calls:** a host MAY subject them to user approval, but isn't
  required to.
- **Tool result size:** the spec says nothing about it.

Rows marked "observed" come from our own runs, not from documentation (note
465798). The ADR depends on that note **only** for those rows. Every other
claim cites code, a PR or public documentation.

### Problems the POC exposed

1. **Bulk bytes travel as base64 in tool results.** Up to 25 MB, twice per
   preview, through every hop.
   - **claude.ai's limits:** a ~150,000-character tool result limit and a
     240 s timeout (claude.com/docs/connectors/building).
   - **What happens above the limit:** the MCP Apps troubleshooting page
     describes it narrowly. With the code-execution sandbox active, an
     oversized result is written to a file, and the view gets a pointer instead
     of `structuredContent`.
   - **What Claude recommends:** app-initiated tool calls to load more data,
     with base64 payloads deferred to a separate tool.
   - **Still undocumented:** whether the limit also applies to app-initiated
     calls. It hasn't been measured either.
2. **The view can't be cached.** A ~2 MB resource under a URI that never
   changes, rebuilt at import, re-read per open.
3. **The viewer requires the vector index.** Geometry comes from Qdrant, so an
   unindexed file, or an instance without Qdrant, gets no viewer.
4. **The host is hard-wired to one view.** `VIEW_URI` is a constant in
   `McpAppHost.vue`, and `callTool` requires `file_id`.
5. **Confirmation depends on the host.** `apply_changes` asks through
   elicitation only when the client supports it. No observed host does, and
   claude.ai doesn't confirm either. Today the risk is low because the tool only
   writes a new copy.
6. **No contract covers the boundary both repos own.** Astrolabe reads
   `_meta.ui.resourceUri`, `_meta.ui.visibility`, `nextcloud-mcp/scopes` and
   `annotations.readOnlyHint` from our `tools/list`, plus the result shapes of the
   app tools. A renamed key or a dropped annotation changes Astrolabe's behaviour
   silently. For example, dropping `read_only_hint` from `preview_changes`
   would make every preview ask for confirmation.

## Decision drivers

- **Portable first.** A view must work in any spec-compliant host. Astrolabe
  features are optional extras, never prerequisites.
- **Degrade, don't disappear.** Every launch tool is useful as text in a host
  without apps support.
- **The host owns trust.** The sandbox, auth and confirmation stay on the host.
  The server never assumes a host confirmed anything it can't verify.
- **Generalise from two examples, not one.** Shared code is extracted when the
  second view needs it.
- **Contracts catch upgrades.** A dependency bump in either repo must fail a
  test before it fails a user.

## Decision

### 1. Vocabulary

- **View**: a `ui://nextcloud-mcp/<name>` resource with
  `mimeType: text/html;profile=mcp-app`.
- **Launch tool**: model-visible, bound to a view through
  `_meta.ui.resourceUri`. Calling it opens the view.
- **App tool**: `visibility=["app"]`, bound to the same view, callable only by
  that view through the host.
- **Host**: the client that renders the view and proxies its requests:
  Astrolabe, claude.ai, Le Chat, ….

### 2. Server: one registry, views declared alongside their tools

- `viewer_tool(*scopes)` becomes `app_tool(view, *scopes, **kwargs)`. It keeps
  today's behaviour: enforce `@require_scopes`, publish
  `_meta["nextcloud-mcp/scopes"]`, and add the scopes to the view's union.
  - It **requires** an explicit `effect="read" | "copy" | "in_place"` and
    raises at import time without one.
    - `effect` sets `annotations.read_only_hint` (true only for `"read"`), so
      the hint hosts use to classify writes (§4) cannot drift from the
      declaration.
    - `effect` also selects the confirmation rule in §4: `"copy"` may proceed
      without elicitation, while `"in_place"` fails closed.
    - It is published as `_meta["nextcloud-mcp/effect"]` so a host can show the
      difference.
  - The `BaseResponse` rule in `CLAUDE.md` still applies to the
    structured content. App tools keep returning `CallToolResult` only where
    they need both text and `structuredContent`.
- One `register_view(name, html_path, *, csp=None)` builds every view's
  resource the same way:
  - inline vendored assets after the hash check;
  - set the `_meta.ui` block, the scopes union and a content version (§4);
  - build the resources **lazily**: `register_view` only records the view,
    and the resources are built when `_app_extensions()` assembles the extension
    at server start. By then every `app_tool` has been imported, so the scopes
    union is always complete.
  - *Background:* today the union depends on a "keep this last in the module"
    comment, guarded by
    `test_the_viewer_resource_publishes_every_scope_its_tools_need`
    (`tests/unit/server/test_document_viewer.py`). The comment goes away, and
    the test stays as a regression guard.
- **Each view gates itself.** The global "only with vector sync" switch becomes
  a per-view requirement, evaluated like capability gating (fail-open, see
  `capabilities.py`). The document viewer gets a non-indexed mode (problem 3):
  render the file without chunk outlines or passages, and treat geometry as an
  enhancement.
- **Layout.** Each view gets a package, `nextcloud_mcp_server/apps/<view>/`,
  with `view.html`, `tools.py` and vendored assets. Domain logic such as
  redaction moves out of the view module into its own package (for example
  `redaction/`), so non-UI callers like the SAR export (ADR-040) can share it.

### 3. The portable host contract (what a view may assume)

**Tier 1, required.** A view must be fully usable with only:

- `ui/initialize` and `ui/notifications/initialized`;
- `ui/notifications/tool-input` and `ui/notifications/tool-result` for its
  launch tool;
- `tools/call` for the tools bound to its own URI;
- `ui/notifications/size-changed`.

**Tier 2, optional.** A view feature-detects each of these (from `hostContext`
or a rejected request) and hides the matching UI when it is missing:

- `ui/message`;
- `ui/update-model-context`;
- `ui/request-display-mode`;
- `host-context-changed` theming.

**Self-contained.** A view makes **no network requests**. Every script, style,
font and wasm module is inlined at serve time, and data arrives only through
tool calls. `_meta.ui.csp.resourceDomains` may still be declared for hosts that
honour it, but nothing may depend on it. This turns the PR #1913 workaround into a
rule.

**Narrow-first layout.** Views are designed for ~360 px inline and grow into
fullscreen when the host offers it.

### 4. Nextcloud extensions to the contract (namespaced, optional for hosts)

The keys live under `nextcloud-mcp/` in `_meta`. A host that ignores them must
still work.

| Key | On | Meaning |
|---|---|---|
| `nextcloud-mcp/scopes` | each app/launch tool; the view (union) | Scopes a proxy should request for that call (exists today) |
| `nextcloud-mcp/version` | the view **and** every tool bound to it | Hash of the HTML **as served**, after inlining, so a vendored-asset bump changes it. *New* |
| `nextcloud-mcp/effect` | each app/launch tool | `read`, `copy` or `in_place` (§2). Informational for hosts. *New* |

**How a host checks freshness without the payload.**
- Copying the version onto the bound tools puts it in `tools/list`, which a
  host already reads (Astrolabe caches it in `ViewTools`) and which is small.
- The host compares the launch tool's `version` against its cached
  `(uri, version)` entry, and calls `resources/read` only on a miss.
- Reading the version from `resources/read` itself would save nothing, so that
  is not the mechanism.

Write classification stays standard: a tool **without**
`annotations.readOnlyHint: true` is a write, and a host should confirm it.
Astrolabe does this today, and the rule becomes part of the contract the
server promises to keep. The server side of that promise is `effect` (§2).

**Confirmation rule (server side):**

- **Copy-only writes** may proceed without any confirmation when elicitation
  is unavailable. Example: `apply_changes`, which creates a new file and never
  modifies the source.
  - **Not every host confirms.** The 2026-01-26 spec only says hosts MAY
    require approval. The draft adds non-normative guidance to approve tools
    with `readOnlyHint: false`. claude.ai neither documents nor showed a
    confirmation, so in claude.ai the user's
    click on "Save copy" in the view is the only confirmation.
  - **Why that's acceptable:**
    - nothing existing is changed or lost;
    - the copy lands beside the original, where the user can see it;
    - saves are create-only, so they never overwrite. An unnamed copy steps
      `-redacted`, `-v2`, `-v3`… up to a cap (`MAX_COPY_SUFFIX`), and a name the
      user typed fails if it's taken.
  - **What it limits:** repeated saves can produce several copies, but the cap
    bounds how many.
  - **`effect="copy"` is the author's promise**, and code review has to check
    it: `app_tool` can't prove a tool never overwrites. Each `copy` tool
    therefore ships with tests that pin both collision paths. For
    `apply_changes` these exist:
    `test_save_steps_past_existing_copies_and_never_overwrites` and
    `test_save_with_an_explicit_name_that_exists_fails_instead_of_renaming`.
- **In-place writes** (`effect="in_place"`: anything that changes or deletes
  existing data) must **fail closed** without elicitation, whether or not a
  host confirmed. `app_tool` enforces this itself, so the rule doesn't depend on
  each tool author remembering it. This covers the future views that edit data
  in place (Deck, Tables).
  - **Consequence, stated deliberately:** no host observed so far, Astrolabe
    included, supports elicitation, so **an `in_place` tool can't run in any
    current host**. That is intended: we won't build in-place editing on an
    unverifiable assumption.
  - **Two ways forward, decided with the first in-place view:**
    - Astrolabe implements elicitation. It's our host, so this is the
      preferred route.
    - Or a confirmation the server can verify itself, such as a short-lived,
      single-use token the server mints when it shows a proposal (the
      propose → apply pattern of #1578), bound to the user, the target and the
      etag.
  - An Astrolabe-only flag the server simply trusts is **not** acceptable.

### 5. Payload rules

- **A launch tool result stays small**: ids, metadata and geometry. Bulk
  content goes through app tools.
- **Bulk content is paged.** Binary reads take a bounded range, for example
  `read_document_bytes(file_id, offset, length)` with a server-side cap per
  call. Previews return only the affected pages where the format allows it. This
  is a **breaking change** to `read_document_bytes` and `preview_changes`. It
  lands with a `BREAKING CHANGE:` commit footer, and its PR names the release
  that carries it ("paged in 0.x.0").
- **Pages are etag-guarded.** Each paged read takes the etag that the launch
  tool returned. If the file changed between pages, the server returns the same
  conflict error as `apply_changes` with a stale etag.
  - The view reloads from `open_document` **at most once** automatically.
  - On a second conflict it stops and tells the user that the file is changing
    and to reopen it, instead of looping on a frequently edited file.
  - **Staged changes are tied to the etag.** Their geometry was computed
    against that version. On a reload with a new etag, the view drops them and
    says so ("the file changed; N staged changes were cleared"). It never
    silently re-applies them to a different version. `preview_changes` and
    `apply_changes` already take the etag, so the server rejects stale
    changes too.
- **Size budget for a view.** A view's `resources/read` stays under **3 MB**
  (today ~2 MB). It is measured on the **serialized `resources/read` JSON
  response**, the wire payload including escaping, not the raw HTML. This
  provisional ceiling gets revisited once host caps on `resources/read` are
  known. It is enforced by a unit test,
  `test_views_stay_within_size_budget`, which builds each registered view and
  checks its size; the harness card adds it.
- **Before fixing the cap**, measure the effective result limit for
  app-initiated `tools/call` in claude.ai and Le Chat (§9).
- **No sensitive content in URLs or link text.** Proposals are passed by id
  (card #1578), not by `redact_text` in viewer links.

### 6. Fallback rules for hosts without apps

Every launch tool returns:

- **Text** that answers the user's question on its own;
- an ADR-035 **deep link** to the same object in Nextcloud/Astrolabe;
- **`structuredContent`** for the view.

`client_supports_apps(ctx)` (python-sdk 2.x, `mcp/server/apps.py`) may be used
to trim the text when a view will render, but never to drop the deep link.

### 7. Astrolabe as a general host

- **Resolve the view from the tool, not a constant.** Take the view URI from the
  launch tool's `_meta.ui.resourceUri` and remove `VIEW_URI` from
  `McpAppHost.vue`.
- **Validate arguments against the schema.** `callTool` checks arguments against
  the tool's `inputSchema` from `tools/list` instead of requiring `file_id`. Any
  file-specific checks (such as `withProposal`) are keyed on the tool name.
- **Cache by version.** Cache the view by `(uri, nextcloud-mcp/version)`, taking
  the version from the launch tool's `_meta` in the cached `tools/list` (§4).
  Today every open costs a new MCP connection plus `resources/read`.
- **Degrade against older servers.** Astrolabe ships on its own cadence, so it
  must work with servers that predate this ADR:
  - no `nextcloud-mcp/version` → don't cache, and read the view on every open
    (today's behaviour);
  - a `read_document_bytes` whose `inputSchema` has no `offset` → the view
    falls back to a single unpaged read;
  - an unknown tool → the existing 404/refusal path.
- **Document `ui/message`.** The Assistant answers it with a separate agent.
  The ADR records this as an Astrolabe choice, not part of the contract. Views
  must not assume the message lands in the user's chat.

### 8. Shared view runtime: deferred until the second view

The bridge (JSON-RPC, handshake, Tier 2 feature detection, theming tokens)
stays inside the document viewer until a second view exists. At that point it
is extracted as `apps/_shared/bridge.js` and inlined at serve time like the
vendored assets. Whether to adopt the ext-apps SDK's `App` class (which needs a
bundling step) is decided then, by comparing it against the hand-rolled bridge
on the second view.

### 9. Contract and end-to-end tests

Three boundaries, each with the right tool.

**A. Astrolabe → MCP server: Pact (both repos).** Astrolabe is the consumer
(`tests/contract/Consumer/`, pact-php driving `mcp/sdk`). The MCP server is the
provider (`test_mcp_provider_verification.py`). Interactions, all JSON-RPC over
`POST /mcp`:

| Interaction | Pins |
|---|---|
| `initialize` advertising `io.modelcontextprotocol/ui` | Extension negotiation |
| `tools/list` | For each app-bound tool: `_meta.ui.resourceUri`, `_meta.ui.visibility`, `_meta["nextcloud-mcp/scopes"]`, `_meta["nextcloud-mcp/version"]`, `_meta["nextcloud-mcp/effect"]`, `annotations.readOnlyHint`, `inputSchema` shape (type matchers, not literal descriptions) |
| `resources/read` `ui://…` | `mimeType`, `_meta.ui.csp`, `_meta["nextcloud-mcp/scopes"]`, `_meta["nextcloud-mcp/version"]`, non-empty `text` |
| `tools/call open_document` | `structuredContent` keys the host reads (`file_id`, `etag`, staged changes) |
| `tools/call apply_changes` with a stale etag | Error shape (`isError`, conflict text) |

Provider states: "file N is an indexed PDF", "file N changed since etag E".

*Known obstacles, to resolve in the first Pact card:*

1. **Authentication.** `tools/list` is filtered by token scopes, and the
   authenticated surface waits on ADR-029 phase 4 (the Bearer-token/provider-state
   hook). This is the critical dependency.
   - **Interim:** verify against a provider started in single-user mode
     (`NEXTCLOUD_USERNAME` / app password, no OAuth). There every tool is
     visible, so the metadata keys (`_meta.ui.*`, `nextcloud-mcp/scopes`,
     `readOnlyHint`) get pinned before phase 4 lands. Those are the keys most
     likely to regress silently.
   - **What the interim misses:** the scope filtering itself stays unverified
     until phase 4.
2. **Session and streaming.**
   - **Today the provider always answers SSE.** It runs stateful, with
     `Mcp-Session-Id`, because `app.py` passes neither option to
     `streamable_http_app`.
   - **Regex-matching session ids doesn't work.** A replayed id gets a 404
     from a stateful server.
   - **Proposed:** verify against a provider started with
     `stateless_http=True, json_response=True` (python-sdk 2.3.0 app-factory
     options).
     - These are set by a **test-only app factory** in `tests/contract/`, which
       calls `get_app()` with those options. There is no environment flag, so
       a deployment can't turn the mode on by accident. That matters because it
       also makes `client_supports_apps()` false (item 3).
     - In this mode each request stands alone, needs no `initialize`, and gets
       plain JSON. A stateless server ignores `Mcp-Session-Id`.
3. **Stateless mode loses client capabilities.** The stateless transport is
   built without the client's `initialize` capabilities, so
   `client_supports_apps()` is always false there.
   - Nothing calls it today. Any path that branches on the client advertising
     `io.modelcontextprotocol/ui` (§6) must be covered by e2e, not by Pact.
   - The `initialize` exchange (extension and `protocolVersion`) is pinned as its
     own interaction.
4. **Interaction order is a non-issue once stateless.** Astrolabe's `mcp/sdk`
   (0.10, protocol 2025-11-25) runs `initialize` → `notifications/initialized` →
   one operation → `DELETE` (only if the server issued a session) for every
   call. Each operation is recorded as its own interaction:
   - the body is matched on `method` and the key params;
   - the client's real `Accept: application/json, text/event-stream` is
     recorded;
   - `notifications/initialized` is a 202 with an empty body;
   - `DELETE` stays out of the contract.
5. **Bearer token.** One token is minted before verification and injected with
   the verifier's custom-header option (`add_custom_header` in pact-python,
   `CustomHeaders` in pact-php). The consumer side matches it with a regex,
   `Bearer .+`, as `McpServerClientPactTest` already does. A per-state token
   would need a custom state-change endpoint, because pact-python's built-in
   handler discards the state handler's return value.

Moving Astrolabe to protocol 2026-07-28 would remove the need for the
stateless workaround. That protocol drops sessions and the handshake, so every
request stands alone, and it adds an `Mcp-Method` header to match on.

**B. View ↔ host postMessage: e2e, not Pact.** Two pieces, both tied to the
apps spec version, so a spec bump fails both sides:

- **MCP server side**: a fake-host harness (an iframe with
  `sandbox="allow-scripts"` and claude.ai's restrictive CSP) loads each view in
  Playwright under Chromium and Firefox, answers Tier 1 only, and asserts the
  view renders. This proves the Tier 1 and self-contained rules. It is the
  scratch technique from PR #1913, made permanent. It explicitly asserts that
  a scanned (JPEG 2000 / JBIG2) page renders. That is the wasm-decoder path from
  PR #1924, and it depends on the host CSP allowing wasm compilation.
- **Astrolabe side**: `npm run test:e2e` drives `McpAppHost.vue` against the
  real view from the docker-compose stack.

**C. MCP server → Nextcloud.** Unchanged: integration lanes against a real
Nextcloud (ADR-029, *Upstream Nextcloud apps*).

### 10. Exit criteria for the validation phase (Proposed → Accepted)

- [ ] A second view built on `app_tool` / `register_view`. Candidates: SAR
      case review, search-results map (PCA coordinates already exist), Deck
      board.
  - It may be read-only or copy-only.
  - §4's fail-closed path is exercised regardless, by a unit test: an
    `effect="in_place"` tool is refused without elicitation, and runs after an
    accepted elicitation from the MCP Inspector.
  - A live in-place view waits for one of the two routes in §4.
- [ ] Both views pass the Tier 1 harness (§9B) and are checked by hand in
      Astrolabe and claude.ai.
  - Le Chat is **best-effort**, because Mistral doesn't document MCP Apps in
    the chat UI.
  - "Pass" there means the launch tool renders the view, or the text and
    deep-link fallback (§6) if it doesn't.
  - A Le Chat failure is recorded, not blocking.
- [ ] Pact §9A green in both repos' CI (shadow mode is acceptable).
- [ ] The app-initiated result limit measured per host, and the paging cap
      in §5 set from it.
- [ ] §8 decided (shared bridge vs ext-apps SDK) and recorded here.

### 11. Follow-up work (cards created once this ADR is reviewed)

Listed so the gap between Proposed and Accepted is visible, not silent:

| Work | Board | Covers |
|---|---|---|
| Pact: Astrolabe ⇄ MCP JSON-RPC surface (consumer + provider, test-only stateless app factory) | 11 | §9A |
| View harness: sandboxed Playwright host, Tier 1 only, scanned-page case, size-budget test | 11 | §9B, §5 |
| `app_tool` / `register_view` + `effect` + `nextcloud-mcp/version` | 12 | §2, §4 |
| Paged, etag-guarded `read_document_bytes` / `preview_changes` (breaking) | 12 | §5 |
| Viewer without the index (no chunk geometry) | 12 | §2, problem 3 |
| Astrolabe general host (per-URI, schema validation, version cache, degradation) | 12 | §7 |
| Re-test claude.ai `resourceDomains` with a minimal view | 12 | Open question 4 |

## Alternatives considered

- **Serve views from a plain HTTP route.** Hosts load `ui://` resources over the
  MCP session and won't iframe arbitrary URLs. The view would also need its own
  auth, CORS and token handling. Rejected. Astrolabe's view route is the host
  side of the same idea and stays.
- **Per-host builds of a view.** Divergence with no upper bound. The tier model
  plus feature detection covers the observed differences.
- **Adopt the ext-apps SDK now.** Adds a JS build step to the server for one
  view. Deferred to §8.
- **Pact message contracts for postMessage.** Possible, but the counterpart is a
  spec-defined protocol rather than a service we version, and a browser harness
  tests what actually breaks (CSP, sandbox, rendering). Rejected in favour of §9B.
- **Expose app tools to the model** (for example, a headless save). Out of scope
  here. The headless redaction design is card #1578, and its fail-closed rule
  matches §4.

## Consequences

- The document viewer moves package and its bulk tools change signature.
  These are breaking changes, recorded with `BREAKING CHANGE:` footers and
  named release versions. Astrolabe adapts to what the server advertises
  (§7, *Degrade against older servers*) rather than relying on deploy order.
- Astrolabe loses its single-view assumptions. Each new view then needs no host
  change unless it uses a Nextcloud extension.
- Pact coverage of the MCP JSON-RPC surface depends on ADR-029 phase 4. Until
  then, §9A covers only what can run without an authenticated provider state,
  and the gap stays tracked on board 11.
- Views carry their dependencies, so their size grows. Version-keyed caching
  (§4, §7) is the mitigation, and a hard size budget per view is left to the
  validation phase.

## Open questions

1. Does claude.ai or Le Chat apply the documented tool result cap to
   app-initiated `tools/call`? This sets the §5 paging cap.
2. Should `nextcloud-mcp/version` instead be a version component in the URI
   (`ui://nextcloud-mcp/document-viewer.html?v=<hash>`)? That makes caching
   automatic on every host, at the cost of tools pointing at a changing URI.
   Leaning towards: `_meta` is enough while Astrolabe is the only host known to
   cache views. Switch to the URI form only if a host we can't modify is shown
   to cache by URI. Decide before the first `register_view` PR.
3. Will MCP standardise per-tool scopes? Neither the apps spec nor the core
   draft has a field today. Core relies on `insufficient_scope` step-up, and a
   tool-scopes working group exists (SEP-1302). Migrate
   `nextcloud-mcp/scopes` if a standard field lands.
4. Why did claude.ai block cdnjs despite `resourceDomains` on the content item
   (PR #1913)? Its docs say the field is honoured. Re-test with a minimal view
   before treating "views are self-contained" as a claude.ai workaround. The
   rule stands regardless: it is the only form that works on every host.
5. For views shown inline in the Assistant chat, should a newer `open_document`
   result update the existing widget in place (note 465798, 2026-11-24)? That is
   a host feature outside the spec, and it may belong in §7.
