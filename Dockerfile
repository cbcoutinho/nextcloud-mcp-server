FROM ghcr.io/astral-sh/uv:0.8.23-python3.11-alpine@sha256:e2079eb6524d4b2afdfe8dadae1e7340813d751447356b37d0ed7386f60d6c40

WORKDIR /app

COPY . .

RUN uv sync --locked --no-dev

ENTRYPOINT ["/app/.venv/bin/nextcloud-mcp-server", "--host", "0.0.0.0"]
