# ADR-035: Deep links from tool responses back to Nextcloud

**Status:** Accepted
**Date:** 2026-08-12
**Supersedes:** none
**Related:** ADR-017 (tool annotations), PR #1273 (`SemanticSearchResult.url`)

## Context

`nc_semantic_search` was the only tool that gave a user a way back to the source
of an answer: `SemanticSearchResult.url` carries a deep link into the Astrolabe
chunk viewer, built by `astrolabe_links.chunk_url()`. Every other tool returned
ids and titles with no way to reach the content in Nextcloud — an agent could say
"your Q3 plan note mentions X" but could not offer to open it.

We wanted the same affordance for the Notes, Files and Deck tools.

## Decision

**Deep links are a `url` field on the response models, populated by a
`@with_links` decorator (`nextcloud_mcp_server/links.py`).**

Two sub-decisions carry the weight.

### 1. A response field, not MCP `_meta`

The obvious home for "metadata a host renders as UI" is `_meta` on the tool
result. We rejected it **for now**, because no client consumes it:

- pydantic-ai discards `_meta` in `_map_mcp_tool_result` (pydantic-ai#6613)
- microsoft/agent-framework does not respect `CallToolResult._meta` (#2284)
- openai-agents-python has no support (openai-agents-python#2367)
- Claude Code filters tool metadata before the model sees it (claude-code#9767)
- Claude Desktop reads only `content`, ignoring `structuredContent`
  (blockscout/mcp-server#324), and does not surface resource entries
  (claude-ai-mcp#287)

`resource_link` content blocks — the spec-native way to return a link — are
subject to the same gap today.

A plain field reaches every client now, because FastMCP serialises the response
model into **both** `content` (JSON text) and `structuredContent`. It also puts
the link in front of the model itself, which is what lets an agent offer the link
in its reply rather than relying on host chrome.

This is explicitly **not** a judgement that `_meta` is wrong or unavailable. It
is available on the protocol version we target: `Result._meta` has existed since
`schema/2024-11-05/schema.ts`, the 2025-11-25 schema defines it with
`CallToolResult extends Result`, and our pinned `mcp` 1.29
(`LATEST_PROTOCOL_VERSION = "2025-11-25"`) carries it and passes it through
dispatch verbatim. The blocker is purely client support. Adopting it later is
tracked on Deck board 12; the intended key is `com.astrolabecloud.mcp/links`,
legal under the 2025-11-25 naming rules because a prefix is reserved only when
its *second* label is `mcp`/`modelcontextprotocol`.

### 2. A decorator, not population at each construction site

Three responses out of five would have been easy to fill in where they are
built. The other two are why we did not:

`DeckCard` and `DeckCardSummary` carry `stackId` but **no `boardId`**, while
Deck's route is `/apps/deck/board/{boardId}/card/{cardId}`. The board id lives
either on an enclosing model (`BoardOverviewResponse.board_id`,
`DeckStack.boardId`) or in the tool's own arguments (`deck_get_cards(board_id=…)`).
Only a wrapper sees both the returned model and the call's arguments, so the
decorator threads a small context dict down the walk.

The alternative — adding `boardId` to the card models — would change what the
Deck API layer returns to satisfy a presentation concern, and would still leave
~30 construction sites to edit.

`with_links` returns the same model instance it received, so declared return
annotations stay truthful, the advertised `outputSchema` is unchanged, and the
"every tool returns a `BaseResponse` model" rule in `CLAUDE.md` continues to hold
verbatim. (An earlier `_meta` design required returning `CallToolResult` and
would have needed a carve-out there.)

## Consequences

- Adding an app means one registry entry plus one `url` field. A unit test
  asserts every registered model declares the field, so the two cannot drift.
- **A link is either usable or absent.** Missing `file_id`, unknown board, or no
  configured base URL all yield `None` rather than a URL that 404s — the caller
  cannot distinguish a broken link from a working one, but `None` is unambiguous.
  This follows `chunk_url`'s existing contract.
- No new configuration: the base URL comes from `astrolabe_browser_base()`
  (`NEXTCLOUD_PUBLIC_URL` → `NEXTCLOUD_PUBLIC_ISSUER_URL` → `NEXTCLOUD_HOST`).
  Deployments that never set a public URL simply get no links.
- Response schemas gain an optional field — additive and backwards compatible.
- Browser routes hardcode `/index.php`, as `ASTROLABE_APP_PATH` does. These are
  URLs a browser opens, so `BaseNextcloudClient._resolve_url` never sees them and
  the "never hardcode `/index.php`" rule for API paths does not apply.
- `list_directory`'s PROPFIND now requests `oc:fileid`. It previously did not,
  so `FileInfo.file_id` was always `None` for directory listings even though the
  model declared it — a latent gap the file links surfaced. The search PROPFINDs
  already asked for it.

## Alternatives considered

**`_meta` on the tool result** — rejected for now; see above. Tracked for later.

**`resource_link` content blocks** — spec-native and the best long-term answer
for host-rendered links, but not surfaced by clients today, and one block per
item inflates large listings. Revisit alongside `_meta`.

**A Pydantic `computed_field` on each model** — no decorator at all, and the URL
would always be present. Rejected because it cannot work for cards (the board id
is not on the model), so it would have meant two mechanisms instead of one, and
it would couple model serialisation to global configuration.
