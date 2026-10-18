# nextcloud-mcp-sar

Subject access request (SAR) cases and redacted export for
[nextcloud-mcp-server](../../README.md), as a plugin. See
[ADR-040](../../docs/ADR-040-sar-redacted-export.md) for the design.

Installed next to the server, it registers itself through the
`nextcloud_mcp_server.plugins` entry point. It is served only when
`SAR_ENABLED=true`, semantic search is on, and `EMBEDDING_GATEWAY_URL` is
set (names are detected through the gateway's `/v1/ner`). See
[configuration](../../docs/configuration.md) for the settings.

It imports from the server only through `nextcloud_mcp_server.plugin_api`
(`tests/test_sar_plugin_boundary.py` enforces this), so it can live in a
repository of its own.

```bash
uv run pytest packages/nextcloud-mcp-sar/tests
```
