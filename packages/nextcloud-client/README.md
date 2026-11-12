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
