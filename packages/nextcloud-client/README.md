# nextcloud-client

Async Python clients for the Nextcloud APIs, built on `httpx2` and `anyio`. Supported:

- **Files (WebDAV):** read, write, search, tags, versions, trash, comments
- **Calendar and tasks (CalDAV)**
- **Contacts (CardDAV)**
- Notes, Deck, Tables, Talk, Mail, News, Collectives, Cookbook, Sharing, Users, Groups and Shopping List

They are the clients behind
[nextcloud-mcp-server](https://github.com/cbcoutinho/nextcloud-mcp-server).
The clients are released with it and share its version number. Installing them
does **not** pull in the MCP server's dependencies.

```bash
pip install nextcloud-client
```

## Usage

```python
import anyio
from httpx2 import BasicAuth

from nextcloud_client import NextcloudClient


async def main():
    nc = NextcloudClient(
        "https://cloud.example.com",
        "alice",
        BasicAuth("alice", "app-password"),
        password="app-password",  # CalDAV builds its own auth from the raw secret
    )
    async for note in nc.notes.get_all_notes():
        print(note["title"])
    print(await nc.webdav.list_directory("/"))


anyio.run(main)
```

For OAuth, use `NextcloudClient.from_token(base_url, token, username)`.

### TLS and connection reuse

```python
import ssl

NextcloudClient(..., verify=ssl.create_default_context(cafile="ca.pem"))
NextcloudClient(..., verify=False)  # self-signed dev instances only
NextcloudClient(..., keepalive=False)  # fresh connection per request
```

### Long Deck comments

Nextcloud rejects comments over 1000 characters (`HTTP 400 "Message exceeds
allowed character limit of 1000"`). `create_split_comment` posts a longer
message as a thread of up to 10 parts, each prefixed `(i/N)`, with parts 2..N
replying to part 1:

```python
from nextcloud_client.deck import (
    CommentTooLongToSplitError,
    PartialSplitCommentError,
    split_comment,
)

posted = await nc.deck.create_split_comment(card_id, long_text)  # list[DeckComment]
parts = split_comment(long_text)  # the same parts, without posting
```

`CommentTooLongToSplitError` means more than 10 parts would be needed, and
nothing was posted. `PartialSplitCommentError` means a part failed: its
`posted` attribute lists the comments that were posted. Post only the remaining
text, with `parent_id=posted[0].id`, rather than re-sending the whole message.
`nextcloud_client.comments` has the underlying `split_message`,
`measured_length` and `COMMENT_MAX_LENGTH`.

### Metrics and tracing

The clients have no dependency on prometheus or OpenTelemetry. To observe them,
subclass `nextcloud_client.instrumentation.Instrumentation` and install it:

```python
from nextcloud_client import instrumentation


class Hooks(instrumentation.Instrumentation):
    def on_api_call(self, app, method, status_code, duration): ...


instrumentation.hooks = Hooks()
```

## License

AGPL-3.0-only.
