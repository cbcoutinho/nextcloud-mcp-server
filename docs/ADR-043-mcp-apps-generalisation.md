# ADR-043: MCP Apps — what "general" means for views, hosts and the contract

## Status

Proposed — 2026-11-26. **Validation phase**: this ADR fixes the vocabulary,
the rules a view and a host must follow, and the contract tests, but leaves the
shape of the shared code open until a second view has been built against it
(§9). Breaking changes to the viewer tools and the Astrolabe host are allowed
during this phase. They are version-gated as usual, never merge-ordered (see
*Breaking changes* in `CLAUDE.md`).

Tracked on Deck board 12, card #1612 (all card numbers here are Deck cards, not
GitHub issues). Builds on the POC (#1525) and the document-first layout
(#1604). Contract-test work goes on board 11.

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
| Honours `_meta.ui.csp.resourceDomains` | Yes (builds the CSP from it) | **No**: blocked cdnjs, hence the inlined pdf.js (#1913) | Not checked |
| `ui/message` | Side-panel agent, no history | Consent card ("Allow once"), then the chat | Lands in the composer |
| Confirms app-tool writes | Yes: 409 until the user confirms | **No** confirmation before save | Not checked |
| Elicitation | No | No | Not checked |
| Inline width | Host-chosen | ~355 px inline, ~570 px expanded | Expand failed on token expiry (#1602) |
| Hides app-only tools from the model | Yes (AgentLoop filters) | Yes | Yes |

These differences, not the spec, decide what a view can rely on.

### Problems the POC exposed

1. **Bulk bytes travel as base64 in tool results.** Up to 25 MB, twice per
   preview, through every hop. claude.ai documents a ~150,000-character tool
   result cap (note 385998). Whether that cap applies to app-initiated calls is
   unmeasured.
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
- One `register_view(name, html_path, *, csp=None)` builds every view's
  resource the same way:
  - inline vendored assets after the hash check;
  - set the `_meta.ui` block, the scopes union and a content version (§4);
  - keep the registration-order guard that exists today.
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
honour it, but nothing may depend on it. This turns the #1913 workaround into a
rule.

**Narrow-first layout.** Views are designed for ~360 px inline and grow into
fullscreen when the host offers it.

### 4. Nextcloud extensions to the contract (namespaced, optional for hosts)

The keys live under `nextcloud-mcp/` in `_meta`. A host that ignores them must
still work.

| Key | On | Meaning |
|---|---|---|
| `nextcloud-mcp/scopes` | each app/launch tool; the view (union) | Scopes a proxy should request for that call (exists today) |
| `nextcloud-mcp/version` | the view | Content hash of the served HTML. A host may cache the view keyed on `(uri, version)`. *New* |

Write classification stays standard: a tool **without**
`annotations.readOnlyHint: true` is a write, and a host should confirm it.
Astrolabe does this today, and the rule becomes part of the contract the
server promises to keep.

**Confirmation rule (server side):**

- **Copy-only writes** may rely on host confirmation when elicitation is
  unavailable. Example: `apply_changes`, which creates a new file and never
  modifies the source.
- **In-place writes** (anything that changes or deletes existing data) must
  **fail closed** without elicitation, whether or not a host confirmed. This
  covers the future views that edit data in place (Deck, Tables).

### 5. Payload rules

- **A launch tool result stays small**: ids, metadata and geometry. Bulk
  content goes through app tools.
- **Bulk content is paged.** Binary reads take a bounded range, for example
  `read_document_bytes(file_id, offset, length)` with a server-side cap per
  call. Previews return only the affected pages where the format allows it. This
  is a **breaking change** to `read_document_bytes` and `preview_changes`,
  version-gated.
- **Before fixing the cap**, measure the effective result limit for
  app-initiated `tools/call` in claude.ai and Le Chat (§9).
- **No sensitive content in URLs or link text.** Proposals are passed by id
  (card #1578), not by `redact_text` in viewer links.

### 6. Fallback rules for hosts without apps

Every launch tool returns:

- **Text** that answers the user's question on its own;
- an ADR-035 **deep link** to the same object in Nextcloud/Astrolabe;
- **`structuredContent`** for the view.

`client_supports_apps(ctx)` (python-sdk) may be used to trim the text when a
view will render, but never to drop the deep link.

### 7. Astrolabe as a general host

- **Resolve the view from the tool, not a constant.** Take the view URI from the
  launch tool's `_meta.ui.resourceUri` and remove `VIEW_URI` from
  `McpAppHost.vue`.
- **Validate arguments against the schema.** `callTool` checks arguments against
  the tool's `inputSchema` from `tools/list` instead of requiring `file_id`. Any
  file-specific checks (such as `withProposal`) are keyed on the tool name.
- **Cache by version.** Cache the view by `(uri, nextcloud-mcp/version)`. Today
  every open costs a new MCP connection plus `resources/read`.
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
| `tools/list` | For each app-bound tool: `_meta.ui.resourceUri`, `_meta.ui.visibility`, `_meta["nextcloud-mcp/scopes"]`, `annotations.readOnlyHint`, `inputSchema` shape (type matchers, not literal descriptions) |
| `resources/read` `ui://…` | `mimeType`, `_meta.ui.csp`, `_meta["nextcloud-mcp/scopes"]`, `_meta["nextcloud-mcp/version"]`, non-empty `text` |
| `tools/call open_document` | `structuredContent` keys the host reads (`file_id`, `etag`, staged changes) |
| `tools/call apply_changes` with a stale etag | Error shape (`isError`, conflict text) |

Provider states: "file N is an indexed PDF", "file N changed since etag E".

*Known obstacles, to resolve in the first Pact card:*

1. **Authentication.** `tools/list` is filtered by token scopes, and the
   authenticated surface waits on ADR-029 phase 4 (the Bearer-token/provider-state
   hook). This is the critical dependency.
2. **Session and streaming.** Streamable HTTP may answer with
   `text/event-stream` and carries `Mcp-Session-Id`. Pact matches neither well.
   Proposed: provider verification runs the server with JSON responses
   (`Accept: application/json`), and session ids are matched by regex.
   Alternative: a stateless provider mode for verification only.
3. **Interaction order.** Pact replays interactions independently, so each one
   must stand alone. An `initialize` per interaction is acceptable.

**B. View ↔ host postMessage: e2e, not Pact.** Two pieces, both tied to the
apps spec version, so a spec bump fails both sides:

- **MCP server side**: a fake-host harness (an iframe with
  `sandbox="allow-scripts"` and claude.ai's restrictive CSP) loads each view in
  Playwright under Chromium and Firefox, answers Tier 1 only, and asserts the
  view renders. This proves the Tier 1 and self-contained rules. It is the
  scratch technique from #1913, made permanent.
- **Astrolabe side**: `npm run test:e2e` drives `McpAppHost.vue` against the
  real view from the docker-compose stack.

**C. MCP server → Nextcloud.** Unchanged: integration lanes against a real
Nextcloud (ADR-029, *Upstream Nextcloud apps*).

### 10. Exit criteria for the validation phase (Proposed → Accepted)

- [ ] A second view built on `app_tool` / `register_view`, preferably one that
      writes in place, so §4's fail-closed rule is exercised. Candidates: SAR
      case review, search-results map (PCA coordinates already exist), Deck
      board.
- [ ] Both views pass the Tier 1 harness (§9B) and are checked by hand in
      Astrolabe, claude.ai and Le Chat.
- [ ] Pact §9A green in both repos' CI (shadow mode is acceptable).
- [ ] The app-initiated result limit measured per host, and the paging cap
      in §5 set from it.
- [ ] §8 decided (shared bridge vs ext-apps SDK) and recorded here.

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
  These are breaking, version-gated changes, and Astrolabe gates on
  `nextcloud-mcp/version` and tool metadata rather than on deploy order.
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
3. Will the apps spec standardise per-tool scopes? If so,
   `nextcloud-mcp/scopes` should be migrated to the spec's field.
4. For views shown inline in the Assistant chat, should a newer `open_document`
   result update the existing widget in place (note 465798, 2026-11-24)? That is
   a host feature outside the spec, and it may belong in §7.
