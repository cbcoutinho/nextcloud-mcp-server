# Nextcloud MCP Server

[![Docker Image](https://img.shields.io/badge/docker-ghcr.io/cbcoutinho/nextcloud--mcp--server-blue)](https://github.com/cbcoutinho/nextcloud-mcp-server/pkgs/container/nextcloud-mcp-server)

The Nextcloud MCP (Model Context Protocol) server allows Large Language Models (LLMs) like OpenAI's GPT, Google's Gemini, or Anthropic's Claude to interact with your Nextcloud instance. This enables automation of various Nextcloud actions, starting with the Notes API.

## Features

The server provides integration with multiple Nextcloud apps, enabling LLMs to interact with your Nextcloud data through a rich set of tools and resources.

## Supported Nextcloud Apps

| App | Support Status | Description |
|-----|----------------|-------------|
| **Notes** | ✅ Full Support | Create, read, update, delete, and search notes. Handle attachments via WebDAV. |
| **Tables** | ⚠️ Row Operations | Read table schemas and perform CRUD operations on table rows. Table management not yet supported. |

## Available Tools

### Notes Tools

| Tool | Description |
|------|-------------|
| `nc_get_note` | Get a specific note by ID |
| `nc_notes_create_note` | Create a new note with title, content, and category |
| `nc_notes_update_note` | Update an existing note by ID |
| `nc_notes_append_content` | Append content to an existing note with a clear separator |
| `nc_notes_delete_note` | Delete a note by ID |
| `nc_notes_search_notes` | Search notes by title or content |

### Tables Tools

| Tool | Description |
|------|-------------|
| `nc_tables_list_tables` | List all tables available to the user |
| `nc_tables_get_schema` | Get the schema/structure of a specific table including columns and views |
| `nc_tables_read_table` | Read rows from a table with optional pagination |
| `nc_tables_insert_row` | Insert a new row into a table |
| `nc_tables_update_row` | Update an existing row in a table |
| `nc_tables_delete_row` | Delete a row from a table |

## Available Resources

| Resource | Description |
|----------|-------------|
| `nc://capabilities` | Access Nextcloud server capabilities |
| `notes://settings` | Access Notes app settings |
| `nc://Notes/{note_id}/attachments/{attachment_filename}` | Access attachments for notes |

### Note Attachments

This server supports adding and retrieving note attachments via WebDAV. Please note the following behavior regarding attachments:

* When a note is deleted, its attachments remain in the system. This matches the behavior of the official Nextcloud Notes app.
* Orphaned attachments (attachments whose parent notes have been deleted) may accumulate over time.
* WebDAV permissions must be properly configured for attachment operations to work correctly.

## Installation

### Prerequisites

*   Python 3.8+
*   Access to a Nextcloud instance

### Local Installation

1.  Clone the repository (if running from source):
    ```bash
    git clone https://github.com/cbcoutinho/nextcloud-mcp-server.git
    cd nextcloud-mcp-server
    ```
2.  Install the package (if running as a library):
    ```bash
    poetry install
    ```

### Docker

A pre-built Docker image is available: `ghcr.io/cbcoutinho/nextcloud-mcp-server`

## Configuration

The server requires credentials to connect to your Nextcloud instance. Create a file named `.env` (or any name you prefer) in the directory where you'll run the server, based on the `env.sample` file:

```dotenv
# .env
NEXTCLOUD_HOST=https://your.nextcloud.instance.com
NEXTCLOUD_USERNAME=your_nextcloud_username
NEXTCLOUD_PASSWORD=your_nextcloud_app_password_or_login_password
```

*   `NEXTCLOUD_HOST`: The full URL of your Nextcloud instance.
*   `NEXTCLOUD_USERNAME`: Your Nextcloud username.
*   `NEXTCLOUD_PASSWORD`: **Important:** It is highly recommended to use a dedicated Nextcloud App Password for security. You can generate one in your Nextcloud Security settings. Alternatively, you can use your regular login password, but this is less secure.

## Running the Server

### Locally

Ensure your environment variables are loaded, then run the server using `mcp run`:

```bash
# Load environment variables from your .env file
export $(grep -v '^#' .env | xargs)

# Run the server
mcp run --transport sse nextcloud_mcp_server.server:mcp
```

The server will start, typically listening on `http://0.0.0.0:8000`.

### Using Docker

Mount your environment file when running the container:

```bash
docker run -p 127.0.0.1:8000:8000 --env-file .env --rm ghcr.io/cbcoutinho/nextcloud-mcp-server:latest
```

This will start the server and expose it on port 8000 of your local machine.

## Usage

Once the server is running, you can connect to it using an MCP client like `uvx`. Add the server to your `uvx` configuration:

```bash
uvx mcp add nextcloud-mcp http://localhost:8000 --default-transport sse
```

You can then interact with the server's tools and resources through your LLM interface connected to `uvx`.

## References:

- https://github.com/modelcontextprotocol/python-sdk

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests on the [GitHub repository](https://github.com/cbcoutinho/nextcloud-mcp-server).

## License

This project is licensed under the AGPL-3.0 License. See the [LICENSE](./LICENSE) file for details.

[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/cbcoutinho-nextcloud-mcp-server-badge.png)](https://mseep.ai/app/cbcoutinho-nextcloud-mcp-server)
