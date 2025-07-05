FROM ghcr.io/astral-sh/uv:0.7.19-python3.11-alpine@sha256:03f28e0c1ffc6dd8061a4e79c890bfd0441fb9ec1d648ce8ad5abfcaaa68789a

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

CMD ["/app/.venv/bin/mcp", "run", "--transport", "sse", "/app/nextcloud_mcp_server/server.py:mcp"]
