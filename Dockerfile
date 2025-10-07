FROM ghcr.io/astral-sh/uv:0.8.24-python3.11-alpine@sha256:50de0388f8c809e9b5ad8b0bed917f02db04a8b8bdf2a810e302e7a133c68273

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
