# Social App

Tools for [Nextcloud Social](https://github.com/nextcloud/social), the
ActivityPub (fediverse) app: an agent with a Nextcloud account can read its
timelines and notifications, post and reply, follow accounts, favourite and
boost. Social implements the Mastodon client API, so the tools, ids and paging
behave the way Mastodon's do. Written against Social v0.24.1.

### Requirements

- **The user needs a Social account (actor).** Social acts through it. Its API
  tries to create one on first use; where that does not happen, calls are
  refused with a 401 — open Social once in the browser as that user or run
  `occ social:account:create <uid>`.
- **Basic auth (app password) works only with `OCS-APIRequest: true`.** The MCP
  server always sends it. Without it Social answers every call with
  `401 {"error": "the access_token was revoked"}`, because it only accepts a
  Basic-auth caller whose request passes Nextcloud's CSRF check.

### Social Tools

| Tool | Scope | Description |
|------|-------|-------------|
| `nc_social_get_my_account` | `social.read` | Your own account |
| `nc_social_get_timeline` | `social.read` | `home` (who you follow), `local` (this server) or `public` timeline |
| `nc_social_get_hashtag_timeline` | `social.read` | Statuses carrying a hashtag |
| `nc_social_get_status` | `social.read` | One status |
| `nc_social_get_status_context` | `social.read` | The thread around a status (ancestors and replies) |
| `nc_social_get_account_statuses` | `social.read` | Statuses one account posted |
| `nc_social_get_account` | `social.read` | An account by numeric id or handle |
| `nc_social_search_accounts` | `social.read` | Search accounts by name or handle |
| `nc_social_get_relationships` | `social.read` | Following / followed-by / requested, per account |
| `nc_social_get_followers` / `nc_social_get_following` | `social.read` | Follower and following lists |
| `nc_social_get_notifications` | `social.read` | Mentions, follows, favourites, boosts, ... |
| `nc_social_post_status` | `social.write` | Post a status or reply; `@handle` in the text mentions someone |
| `nc_social_delete_status` | `social.write` | Delete one of your own statuses |
| `nc_social_follow_account` / `nc_social_unfollow_account` | `social.write` | Follow / unfollow |
| `nc_social_favourite_status` / `nc_social_unfavourite_status` | `social.write` | Favourite / undo |
| `nc_social_boost_status` / `nc_social_unboost_status` | `social.write` | Boost / undo (public statuses only) |

### Visibility

`nc_social_post_status` takes `public`, `unlisted`, `private` (followers only) or
`direct` (only the mentioned accounts). **Leave `visibility` out to use the
account's own default**, which the account owner sets in Social; the tool never
picks one for you.

### Polling for what is new

Social has no streaming or push API, so an agent polls. Timeline, hashtag,
account-status and notification tools return `cursors` read from Social's `Link`
header:

- `cursors.next_max_id` — pass as `max_id` for the next, older page.
- `cursors.prev_min_id` — pass as `min_id` on a later call to get only what
  arrived since. It is null on an empty page; keep the cursor you had.

```
nc_social_get_notifications(types=["mention"])            # first read
nc_social_get_notifications(types=["mention"], min_id="1234")  # later: only newer
```

Pages hold at most 50 items.

### Status text

Statuses carry their text as plain text (`text`), converted from Social's HTML.
A boost is a status whose `reblog` holds the boosted post.
