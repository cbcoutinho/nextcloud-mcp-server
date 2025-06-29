FROM ghcr.io/astral-sh/uv:0.7.17-python3.11-alpine@sha256:99e71b85ff6382a9853c3504f549a52ca7742a89c8bf5681487f80528f8e094a

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

CMD ["/app/.venv/bin/mcp", "run", "--transport", "sse", "/app/nextcloud_mcp_server/server.py:mcp"]
