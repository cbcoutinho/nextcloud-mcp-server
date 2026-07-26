# Fork notes (commercedeployer / aimaco)

Upstream: [cbcoutinho/nextcloud-mcp-server](https://github.com/cbcoutinho/nextcloud-mcp-server)

Base tag for this branch: **v0.151.0**  
Branch: `aimaco/v0.151.0-talk-create`  
Tracking issue: https://github.com/cbcoutinho/nextcloud-mcp-server/issues/1166

## Delta vs upstream

| Change | Why |
|--------|-----|
| MCP tool `talk_create_conversation` | Agents must open one-to-one / group Talk rooms without a pre-shared room token. Upstream client already had `TalkClient.create_conversation` but did not register it as a tool. |
| Model `CreateConversationResponse` | Response wrapper for the new tool. |
| Client docstring | Notes that the method is exposed as an MCP tool in this fork. |

## How aimaco consumes this

Hub image installs this branch over the pinned upstream base image (`uv pip install --force-reinstall` from this git ref). See `aimaco/hub/Dockerfile` and `aimaco/docs/NEXTCLOUD-MCP-RU.md`.

## Sync checklist (when bumping)

1. Fetch upstream release tag (e.g. `v0.152.0`).
2. Rebase or merge onto that tag.
3. Check whether upstream already added `talk_create_conversation` (or equivalent). If yes — drop our delta and point hub at upstream again.
4. Retest: create one-to-one with `invite=<user>`, then `talk_send_message` with returned `token`.
