# Fork notes (commercedeployer / aimaco)

Upstream: [cbcoutinho/nextcloud-mcp-server](https://github.com/cbcoutinho/nextcloud-mcp-server)

Base tag for this branch: **v0.151.0**  
Branch: `aimaco/v0.151.0-talk-create`  
Tracking issue: https://github.com/cbcoutinho/nextcloud-mcp-server/issues/1166

## Delta vs upstream

| Change | Why |
|--------|-----|
| MCP tool `talk_create_conversation` | Open one-to-one / group / public rooms without a pre-shared token. |
| MCP tool `talk_add_participant` | Invite more users into an existing **group/public** room (3-way, all-hands, reports). |
| MCP tools `talk_react` / `talk_list_reactions` / `talk_delete_reaction` | Emoji reactions (see / react / remove) without leaving Talk. |
| TalkMessage optional `parent`, `reactions`, `reactionsSelf` | Agents see reply threads and reaction counts on `talk_get_messages`. |
| Models `CreateConversationResponse`, `AddParticipantResponse`, reaction responses | Response wrappers. |
| Client `add_participant`, reaction helpers | OCS invite + `/reaction/{token}/{messageId}`. |

## Agent usage (rooms)

| Need | How |
|------|-----|
| DM with one colleague | `talk_create_conversation` room_type=1, invite=`hermes-cto` |
| Private group (3+) | room_type=2 + room_name, then `talk_add_participant` for each login |
| Public / reports channel | room_type=3 + room_name, optionally add members |
| Threaded reply | `talk_send_message` with `reply_to=<message id>` |
| Emoji react | `talk_react` token + message_id + emoji; list with `talk_list_reactions` |

Never treat a type=1 DM token as a shared room — other users get 404.

## How aimaco consumes this

Hub image installs this branch over the pinned upstream base image (`uv pip install --force-reinstall` from this git ref). See `aimaco/hub/Dockerfile` and `aimaco/docs/NEXTCLOUD-MCP-RU.md`.

## Sync checklist (when bumping)

1. Fetch upstream release tag (e.g. `v0.152.0`).
2. Rebase or merge onto that tag.
3. If upstream already has create + add-participant — drop our delta.
4. Retest: group create → add 2 users → send message; DM create still works.
